"""把 HTTP 请求信息绑定到审计上下文的 ASGI 中间件。

职责边界：只做「读取请求头 → 绑定 contextvars」，不参与鉴权与审计写入。

WHY 用纯 ASGI 中间件而不是 ``BaseHTTPMiddleware``：后者会把下游应用放进
独立的 anyio 任务组并拦截响应体，对 SSE 这类长连接流式响应有已知副作用
（响应被缓冲、客户端断开检测延迟）。本中间件只在调用下游前后各做一次
轻量操作，且不触碰 ``send``，对流式响应零影响。
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from application.audit_context import bind_request_context, reset_request_context
from interfaces.web.auth.utils import client_ip, user_agent

logger = logging.getLogger(__name__)


class RequestContextMiddleware:
    """为每个 HTTP 请求绑定客户端 IP 与 User-Agent。

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
        token = bind_request_context(ip=client_ip(request), user_agent=user_agent(request))
        try:
            await self._app(scope, receive, send)
        finally:
            # WHY 无条件回滚：请求任务可能被复用或长期存活（SSE），
            # 泄漏的上下文会让后续审计记录挂到别人的 IP 上。
            reset_request_context(token)


__all__ = ["RequestContextMiddleware"]
