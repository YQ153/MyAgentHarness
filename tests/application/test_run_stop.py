"""运行中止（stop）语义的回归测试。

覆盖面：幂等语义（not_running / already_stopping / requested）、
停止后以 DONE(reason=stopped) 收尾且槽位释放、已产出事件不丢失、
权限与所有权校验、run_cancelled 审计落库。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from application.errors import (
    NotFoundError,
    OwnershipError,
    PermissionDeniedError,
)
from application.events import AgentEventType
from application.run_service import RunService
from runtime import execution_registry as registry_module
from tests.application.test_run_service import (
    FakeGraphFactory,
    SlowGraph,
    _drain,
    _make_service,
    _principal,
)
from tests.conftest import StubSessionRegistry, make_config


class ChunkedThenHangGraph:
    """先产出若干分片、然后长时间挂起的图替身。

    用于验证：停止请求生效时，已产出的分片照常送达前端，
    后续分片不再产出，流以 DONE(reason=stopped) 收尾。
    """

    def __init__(self, chunks: list[tuple[str, Any]], delay: float = 10.0) -> None:
        self._chunks = chunks
        self._delay = delay

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> Any:
        for chunk in self._chunks:
            yield chunk
        await asyncio.sleep(self._delay)


class RecordingAuditStore:
    """记录审计调用的替身，用于断言事件确实落库。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def log(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


def _text_chunk(text: str) -> tuple[str, Any]:
    """构造一条 messages 流分片（与 LangGraph 的 (mode, (message, meta)) 形状一致）。"""
    return ("messages", (AIMessageChunk(content=text), {"langgraph_node": "model"}))


@pytest.fixture(autouse=True)
def _clear_execution_registry():
    """每个用例结束后清空执行登记处。

    WHY 必须清：登记处是**进程级**字典（终止请求来自事件循环线程、终止动作落在
    工作线程，二者只能靠进程级共享状态相遇），而本文件的图替身只登记、不像
    ``host_shell`` 那样在自己的 ``finally`` 里反登记。不清就会串味：前一个用例
    遗留的句柄会被后一个用例的 ``abort_scope`` 命中，表现为计数断言莫名变多。
    """
    yield
    with registry_module._guard:  # noqa: SLF001 - 测试专用清理
        registry_module._registry.clear()  # noqa: SLF001


# ------------------------------------------------------------------ 幂等语义


async def test_stop_idle_thread_returns_not_running(test_config, thread_store):
    service = _make_service(test_config, thread_store)
    await _drain(await service.stream("t1", "hello"))

    result = await service.stop("t1")

    assert result == {"thread_id": "t1", "stopped": False, "reason": "not_running"}


async def test_stop_unknown_thread_raises_not_found(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    with pytest.raises(NotFoundError):
        await service.stop("ghost")


async def test_stop_rejects_blank_thread_id(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    with pytest.raises(ValueError):
        await service.stop("   ")


# ------------------------------------------------------------------ 停止生效路径


async def test_stop_mid_run_ends_with_stopped_done(test_config, thread_store):
    service = _make_service(test_config, thread_store, SlowGraph(delay=10.0))
    generator = await service.stream("t1", "long task")
    collector = asyncio.ensure_future(_drain(generator))
    await asyncio.sleep(0.05)
    assert service.is_running("t1") is True

    result = await service.stop("t1")

    assert result == {"thread_id": "t1", "stopped": True, "reason": "requested"}

    events = await collector
    # 停止不是错误：以 DONE(reason=stopped) 收尾，而非挂起或 ERROR。
    # WHY 序列里多出 USAGE：停止只截断「还没产出的内容」，已经消耗的 token
    # 是既成事实，必须照常上报，否则成本统计会系统性偏低。
    assert [event.event for event in events] == [AgentEventType.USAGE, AgentEventType.DONE]
    assert events[-1].payload["thread_id"] == "t1"
    assert events[-1].payload["reason"] == "stopped"

    # 停止后槽位必须释放，会话可立即重新发起
    assert service.is_running("t1") is False
    again = await _drain(await service.stream("t1", "after stop"))
    assert again[-1].event == AgentEventType.DONE
    assert "reason" not in again[-1].payload


async def test_stop_preserves_already_produced_events(test_config, thread_store):
    graph = ChunkedThenHangGraph(chunks=[_text_chunk("第一段输出")])
    service = _make_service(test_config, thread_store, graph)
    collector = asyncio.ensure_future(_drain(await service.stream("t1", "hi")))
    # 让第一个分片产出并被消费
    await asyncio.sleep(0.05)

    await service.stop("t1")

    events = await collector
    # 已产出的 TOKEN 不丢失；未产出的部分不再等待
    assert [event.event for event in events] == [
        AgentEventType.TOKEN,
        AgentEventType.USAGE,
        AgentEventType.DONE,
    ]
    assert events[0].payload["text"] == "第一段输出"
    assert events[-1].payload["reason"] == "stopped"


async def test_stop_is_idempotent_while_stopping(test_config, thread_store):
    service = _make_service(test_config, thread_store, SlowGraph(delay=10.0))
    # 占用槽位但不消费：句柄保持存活，两次 stop 之间的状态可精确控制
    generator = await service.stream("t1", "hi")

    first = await service.stop("t1")
    second = await service.stop("t1")

    assert first["reason"] == "requested"
    assert second == {"thread_id": "t1", "stopped": True, "reason": "already_stopping"}

    # 收尾：消费生成器让停止信号走完整个收尾路径并释放槽位
    events = await _drain(generator)
    assert events[-1].payload["reason"] == "stopped"
    assert service.is_running("t1") is False


# ------------------------------------------------------------------ 权限与所有权


async def test_stop_requires_permission_and_ownership(tmp_path, thread_store):
    config = make_config(tmp_path, auth_mode="apikey", auth_session_secret="s" * 32)
    service = _make_service(config, thread_store)
    alice = _principal("alice")
    await _drain(await service.stream("t1", "alice's run", principal=alice))

    # viewer 缺少 thread:create 权限
    with pytest.raises(PermissionDeniedError):
        await service.stop("t1", principal=_principal("v", role="viewer"))

    # 其他成员无权停止他人的会话
    with pytest.raises(OwnershipError):
        await service.stop("t1", principal=_principal("bob"))

    # 所有者本人停止空闲会话：幂等成功
    assert (await service.stop("t1", principal=alice))["stopped"] is False


# ------------------------------------------------------------------ 审计


async def test_stop_audits_run_cancelled(test_config, thread_store):
    audit = RecordingAuditStore()
    service = RunService(
        test_config,
        thread_store=thread_store,
        graph_factory=FakeGraphFactory(SlowGraph(delay=10.0)),
        workspaces=StubSessionRegistry(test_config),
        audit_store=audit,
    )
    generator = await service.stream("t1", "audited run")

    await service.stop("t1")

    run_cancelled = [item for item in audit.events if item["event_type"] == "run_cancelled"]
    assert len(run_cancelled) == 1
    assert run_cancelled[0]["action"] == "stop"
    assert run_cancelled[0]["outcome"] == "success"
    assert run_cancelled[0]["target_id"] == "t1"
    assert run_cancelled[0]["actor_id"] == "anonymous"

    await _drain(generator)


# ------------------------------------------------------------------ 工具执行中段取消（D-4）


class RecordingAbortHandle:
    """最小可中止句柄：只记录被要求终止的次数。

    WHY 不用真进程：本用例验证的是「stop → abort_scope → 句柄被终止」这条接线；
    进程树是否真的消失、登记表是否被清理，由 ``tests/runtime/test_host_shell.py``
    的真进程用例负责。两层分工明确，任一层坏掉都会被对应的用例抓住。
    """

    def __init__(self) -> None:
        self.abort_calls = 0

    def abort(self) -> None:
        self.abort_calls += 1


class BlockingToolGraph:
    """复刻「同步工具正在执行」：登记可中止句柄后长时间挂起。

    WHY 走 ``execution_registry.register`` 这个真实入口：真实场景里 ``execute``
    是同步工具，由 LangGraph 的 ``run_in_executor(copy_context)`` 调度到工作线程，
    登记渠道正是这个函数。用同一入口复刻，才能验证作用域确实由 RunService 的
    ``bound_scope`` 绑定——若测试自己造一个作用域，那么「绑错了会话」这类缺陷
    照样能通过。
    """

    def __init__(self, delay: float = 30.0) -> None:
        self.handle = RecordingAbortHandle()
        self.registered = False
        self._delay = delay

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> Any:
        self.registered = registry_module.register(self.handle)
        await asyncio.sleep(self._delay)
        yield  # noqa: WPS328 不可达，仅为构造异步生成器


async def test_stop_aborts_command_running_mid_tool(test_config, thread_store):
    """工具执行中段的停止：已登记的命令句柄必须被真正中止。

    WHY 这条断言在 T7 之前不可能成立：那时 ``stop`` 只置位取消标志，对工作线程里
    正在跑的同步 ``execute`` 毫无影响——真机实测中命令会继续活完自己的寿命，
    而 UI 已经复位，用户以为停了（见开发计划 T2 风险验证）。
    """
    graph = BlockingToolGraph()
    service = _make_service(test_config, thread_store, graph)
    collector = asyncio.ensure_future(_drain(await service.stream("t1", "run a tool")))
    # 等图的第一轮迭代跑过登记那一步，用例前提才成立
    await asyncio.sleep(0.05)
    assert graph.registered is True, "句柄没有登记进作用域：RunService 的 bound_scope 接线失效"
    assert graph.handle.abort_calls == 0

    result = await service.stop("t1")

    assert result == {"thread_id": "t1", "stopped": True, "reason": "requested"}
    assert graph.handle.abort_calls == 1, "stop 没有把终止通知送到正在执行的命令"

    events = await collector
    assert events[-1].event is AgentEventType.DONE
    assert events[-1].payload["reason"] == "stopped"
    assert service.is_running("t1") is False
