"""Web 请求上下文中间件的回归测试。

WHY 直接以 ASGI 三元组调用中间件而不起 TestClient：本中间件只做「读头 →
绑定 → 放行 → 回滚」，不起服务即可完整验证，且不必引入额外的测试依赖。
"""

from __future__ import annotations

from typing import Any

import pytest

from application.audit_context import current_request_context
from interfaces.web.request_context import RequestContextMiddleware


def _http_scope(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    """构造最小的 HTTP ASGI 作用域。"""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/threads/t1/runs",
        "raw_path": b"/api/threads/t1/runs",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 54321),
        "server": ("testserver", 80),
    }


async def _noop_receive() -> Any:  # 仅满足 ASGI 签名：中间件不读请求体
    return {"type": "http.request", "body": b"", "more_body": False}


async def _noop_send(message: dict[str, Any]) -> None:
    return None


def _capture_app(captured: dict[str, Any]) -> Any:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        context = current_request_context()
        captured["ip"] = context.ip
        captured["user_agent"] = context.user_agent

    return app


async def _call_middleware(headers: list[tuple[bytes, bytes]], captured: dict[str, Any]) -> None:
    middleware = RequestContextMiddleware(_capture_app(captured))
    await middleware(_http_scope(headers), _noop_receive, _noop_send)


# ------------------------------------------------------------------ 用例


async def test_middleware_binds_forwarded_ip_and_user_agent():
    captured: dict[str, Any] = {}
    headers = [
        (b"x-forwarded-for", b"203.0.113.9, 10.0.0.1"),
        (b"user-agent", b"Mozilla/5.0 (Windows NT 10.0)"),
    ]

    await _call_middleware(headers, captured)

    # 只取逗号分隔的第一跳：后续是代理链，审计只关心最外层客户端
    assert captured["ip"] == "203.0.113.9"
    assert captured["user_agent"] == "Mozilla/5.0 (Windows NT 10.0)"


async def test_middleware_falls_back_to_client_host():
    captured: dict[str, Any] = {}

    await _call_middleware([(b"user-agent", b"curl/8.5.0")], captured)

    assert captured["ip"] == "127.0.0.1"


async def test_middleware_rolls_back_context_after_response():
    """WHY 本用例是核心契约：SSE 请求的任务若残留上下文，
    同一个事件循环里后续请求的审计会串到它的 IP 上。"""
    captured: dict[str, Any] = {}

    await _call_middleware([(b"x-forwarded-for", b"203.0.113.9")], captured)

    assert current_request_context().ip == ""


async def test_non_http_scope_passes_through_without_binding():
    captured: dict[str, Any] = {}
    middleware = RequestContextMiddleware(_capture_app(captured))

    async def app(scope: Any, receive: Any, send: Any) -> None:
        captured["seen"] = True

    middleware = RequestContextMiddleware(app)
    await middleware({"type": "lifespan"}, _noop_receive, _noop_send)

    assert captured["seen"] is True
    assert current_request_context().ip == ""


async def test_middleware_propagates_downstream_exception_and_resets():
    async def failing_app(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("下游炸了")

    middleware = RequestContextMiddleware(failing_app)

    with pytest.raises(RuntimeError):
        await middleware(_http_scope([(b"x-forwarded-for", b"203.0.113.9")]), _noop_receive, _noop_send)

    assert current_request_context().ip == ""


def test_constructor_rejects_none_app():
    with pytest.raises(ValueError):
        RequestContextMiddleware(None)
