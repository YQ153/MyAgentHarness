"""把 HTTP 请求信息绑定到审计上下文的 ASGI 中间件。

职责边界：只做「读取请求头 → 绑定 contextvars」，不参与鉴权与审计写入。

WHY 用纯 ASGI 中间件而不是 ``BaseHTTPMiddleware``：后者会把下游应用放进
独立的 anyio 任务组并拦截响应体，对 SSE 这类长连接流式响应有已知副作用
（响应被缓冲、客户端断开检测延迟）。本中间件只在调用下游前后各做一次
轻量操作，且不触碰 ``send``，对流式响应零影响。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from application.audit_context import (
    MAX_TRACE_ID_CHARS,
    bind_request_context,
    reset_request_context,
)
from interfaces.web.deps import client_ip, user_agent

logger = logging.getLogger(__name__)

TRACE_HEADER = "X-Request-Id"
"""链路标识的请求/响应头名。"""


def resolve_trace_id(request: Request) -> str:
    """取本次请求的 trace_id：上游带了就沿用，否则现生成一个。

    WHY 沿用上游的 ``X-Request-Id``：链路里已经有 ID 时再造一个，会让网关日志与
    这里的日志各说各话——排查时先要做一次映射，而那正是「可观测」要消除的成本。

    WHY 现生成用 ``uuid4().hex``：跨进程、跨重启都唯一，不需要任何协调。

    WHY 在这里也截断一次：这个值来自客户端，而它会被写进日志行与审计表；
    长度上限与字符过滤在 ``bind_request_context`` 里统一执行，这里只挡住超长头。
    """
    provided = request.headers.get(TRACE_HEADER)
    if provided and provided.strip():
        return provided.strip()[:MAX_TRACE_ID_CHARS]
    return uuid.uuid4().hex


def _with_trace_header(send: Send, trace_id: str) -> Send:
    """包装 ``send``，在响应头里回传本次请求的 trace_id。

    WHY 只改 ``http.response.start``：补一个响应头不需要看响应体一眼，因此对
    SSE 这类流式响应零影响——若改成缓冲响应体去改头，流式输出会退化成一次性返回。
    """

    async def wrapped(message: dict[str, Any]) -> None:
        if message.get("type") == "http.response.start":
            headers = list(message.get("headers") or [])
            headers.append(
                (TRACE_HEADER.lower().encode("latin-1"), trace_id.encode("latin-1", "replace"))
            )
            message = {**message, "headers": headers}
        await send(message)

    return wrapped


class RequestContextMiddleware:
    """为每个 HTTP 请求绑定客户端 IP、User-Agent 与链路标识。

    绑定发生在同一请求任务内：uvicorn 为每个请求创建独立任务，
    ``contextvars`` 的写入对该任务的下游调用链可见，且不会串到并发请求上。

    Args:
        app: 下游 ASGI 应用。

    Raises:
        ValueError: ``app`` 为 ``None``。
    """

    def __init__(self, app: ASGIApp) -> None:
        if app is None:
            raise ValueError("app 不能为 None")
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """绑定上下文后调用下游应用，退出时必然回滚。

        Args:
            scope: ASGI 请求作用域。
            receive: 接收请求体的可调用对象。
            send: 发送响应的可调用对象。

        Raises:
            BaseException: 下游异常原样向上传播；上下文在 ``finally`` 中回滚，
                不掩盖任何异常。
        """
        # WHY 非 HTTP 作用域（lifespan / websocket）直接放行：它们没有
        # 请求头语义，绑定空上下文只会让审计看起来像来自未知来源。
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        trace_id = resolve_trace_id(request)
        token = bind_request_context(
            ip=client_ip(request),
            user_agent=user_agent(request),
            trace_id=trace_id,
        )
        try:
            await self._app(scope, receive, _with_trace_header(send, trace_id))
        finally:
            # WHY 无条件回滚：请求任务可能被复用或长期存活（SSE），
            # 泄漏的上下文会让后续审计记录挂到别人的 IP 上。
            reset_request_context(token)


__all__ = ["RequestContextMiddleware"]
