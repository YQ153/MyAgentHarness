"""链路标识（trace_id）的回归测试。

覆盖面：上游带 ``X-Request-Id`` 时沿用、未带时生成、响应头回传、恶意头被过滤、
并发请求互不串扰、请求结束后上下文回滚、未绑定上下文（CLI 形态）取值为「未知」，
以及同一请求的 trace_id 确实落到了审计与用量两条记录上。

WHY 用迷你 ASGI 应用而不是完整 Web 应用：本文件要验的是中间件与上下文之间的
契约，拉起完整应用（配置、数据库、模型）只会让失败指向环境而不是中间件。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from application.audit_context import (
    audit_trace_id,
    current_request_context,
    request_context,
)
from application.run_service import RunService
from interfaces.web.request_context import (
    TRACE_HEADER,
    RequestContextMiddleware,
    resolve_trace_id,
)
from tests.application.test_run_service import FakeGraphFactory, FakeGraph, _drain
from tests.conftest import StubSessionRegistry


async def _echo(request: Request) -> JSONResponse:
    """把当前上下文里的 trace_id 回显出来，供断言绑定是否生效。"""
    context = current_request_context()
    await asyncio.sleep(0.01)  # 制造交错窗口，验证并发下不串扰
    return JSONResponse({"trace_id": context.trace_id, "ip": context.ip})


def _app() -> Any:
    app = Starlette(routes=[Route("/echo", _echo)])
    app.add_middleware(RequestContextMiddleware)
    return app


def _client() -> httpx.AsyncClient:
    """构造打到迷你应用上的客户端。

    WHY 用异步客户端：``httpx.ASGITransport`` 只实现了异步接口，配同步客户端会在
    发请求时抛 ``AttributeError``——那是客户端用法错误，不是被测代码的问题。
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    )


# ------------------------------------------------------------------ 取值


async def test_upstream_trace_id_is_reused():
    """上游带了 X-Request-Id 就沿用，不再造第二个。"""
    async with _client() as client:
        response = await client.get("/echo", headers={TRACE_HEADER: "gateway-abc-123"})

    assert response.status_code == 200
    assert response.json()["trace_id"] == "gateway-abc-123"
    # 回传的响应头必须与请求里的一致，上游才能把两段日志对上
    assert response.headers[TRACE_HEADER] == "gateway-abc-123"


async def test_missing_trace_id_is_generated():
    """没带就生成一个，并同样回传。"""
    async with _client() as client:
        response = await client.get("/echo")

    generated = response.headers[TRACE_HEADER]
    assert len(generated) == 32  # uuid4().hex
    assert response.json()["trace_id"] == generated


async def test_hostile_trace_id_is_sanitized():
    """客户端可控的头不得把换行带进日志。

    WHY 单列一条：这个值会被拼进日志行与审计表，含换行或制表符的值能凭空多造出
    一条日志记录——那是注入，不是「格式不合法」。
    """
    async with _client() as client:
        response = await client.get(
            "/echo", headers={TRACE_HEADER: "ok\ninjected\tvalue"}
        )

    trace_id = response.json()["trace_id"]
    assert "\n" not in trace_id
    assert "\t" not in trace_id
    assert trace_id.startswith("ok")


async def test_overlong_trace_id_is_truncated():
    async with _client() as client:
        response = await client.get("/echo", headers={TRACE_HEADER: "x" * 500})

    assert len(response.json()["trace_id"]) <= 64


# ------------------------------------------------------------------ 绑定与回滚


async def test_concurrent_requests_do_not_share_trace_ids():
    """并发请求各自持有自己的 trace_id，不互相串扰。"""
    async with _client() as client:
        responses = await asyncio.gather(
            *[
                client.get("/echo", headers={TRACE_HEADER: f"trace-{index}"})
                for index in range(8)
            ]
        )

    assert [item.json()["trace_id"] for item in responses] == [
        f"trace-{index}" for index in range(8)
    ]


async def test_context_is_rolled_back_after_request():
    """请求结束后上下文必须回滚，否则会泄漏到同事件循环里的后续任务上。"""
    async with _client() as client:
        await client.get("/echo", headers={TRACE_HEADER: "trace-one"})

    assert current_request_context().trace_id == ""


def test_unbound_context_means_unknown():
    """未绑定上下文（CLI、后台任务）时取值为「未知」，而不是一个假 ID。"""
    assert audit_trace_id() is None

    with request_context(trace_id="from-cli"):
        assert audit_trace_id() == "from-cli"

    assert audit_trace_id() is None


def test_resolve_prefers_header_over_generation():
    """有头就沿用，没头才生成——两种来源不会同时出现。"""
    scope = {
        "type": "http",
        "headers": [(b"x-request-id", b"from-gateway")],
        "method": "GET",
        "path": "/",
    }
    same = resolve_trace_id(Request(scope))
    assert same == "from-gateway"

    blank = resolve_trace_id(Request({**scope, "headers": [(b"x-request-id", b"   ")]}))
    assert blank != "   "
    assert len(blank) == 32


# ------------------------------------------------------------------ 落到记录上


class _RecordingAudit:
    """只记录调用参数的审计存储替身。"""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def log(self, **kwargs: Any) -> None:
        self.entries.append(kwargs)


class _RecordingUsage:
    """只记录调用参数的用量存储替身。"""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> int:
        self.entries.append(kwargs)
        return len(self.entries)


async def test_one_trace_reaches_audit_and_usage(
    test_config, thread_store, monkeypatch
):
    """同一请求的 trace_id 必须同时出现在审计与用量两条记录上。

    这正是 Sprint 7 门禁要的那句话：只看其中一条，都回答不了「这一次请求
    到底做了什么、花了多少」。
    """
    audit = _RecordingAudit()
    usage = _RecordingUsage()
    service = RunService(
        test_config,
        thread_store=thread_store,
        graph_factory=FakeGraphFactory(FakeGraph()),
        workspaces=StubSessionRegistry(test_config),
        audit_store=audit,
        usage_store=usage,
    )
    await thread_store.create("t1", title="会话")

    with request_context(trace_id="trace-e2e"):
        # 上游流不产出 USAGE 事件，直接喂一条，等价于真实运行时的那一次上报
        await _drain(await service.stream("t1", "你好"))
        await service._record_usage(  # noqa: SLF001 - 直接驱动用量写入这一环
            service._acquire_run_slot("t1"),  # noqa: SLF001
            {"prompt_tokens": 3, "completion_tokens": 4},
        )

    assert {entry["trace_id"] for entry in audit.entries} == {"trace-e2e"}
    assert {entry["trace_id"] for entry in usage.entries} == {"trace-e2e"}


async def test_trace_is_null_outside_a_request(test_config, thread_store):
    """无请求上下文时落 NULL，而不是空串。

    WHY 必须是 ``None``：``WHERE trace_id IS NULL`` 是「找出非请求来源的记录」的
    唯一写法，空串会让这类查询失效。
    """
    audit = _RecordingAudit()
    usage = _RecordingUsage()
    service = RunService(
        test_config,
        thread_store=thread_store,
        graph_factory=FakeGraphFactory(FakeGraph()),
        workspaces=StubSessionRegistry(test_config),
        audit_store=audit,
        usage_store=usage,
    )
    await thread_store.create("t1", title="会话")

    await _drain(await service.stream("t1", "你好"))
    await service._record_usage(  # noqa: SLF001
        service._acquire_run_slot("t1"),  # noqa: SLF001
        {"prompt_tokens": 1, "completion_tokens": 1},
    )

    assert {entry["trace_id"] for entry in audit.entries} == {None}
    assert {entry["trace_id"] for entry in usage.entries} == {None}


@pytest.mark.parametrize("value", [None, 12345])
def test_non_string_trace_id_is_ignored(value: Any):
    with request_context(trace_id=value):
        assert audit_trace_id() is None
