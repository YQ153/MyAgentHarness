"""审计所需的请求上下文（客户端 IP 与 User-Agent）。

WHY 用 ``contextvars`` 而不是把 ``Request`` 一路透传：审计写入发生在应用
服务内部（``RunService._audit`` / ``ThreadService._audit``），把 ``Request``
作为参数传下去会让 ``application`` 层反向依赖 FastAPI，破坏
``interfaces → application`` 的单向依赖；而给每个服务方法加 ``ip`` /
``user_agent`` 两个参数，会让「谁在什么时候写了审计」这件事散落到十几个
调用点，任何一处漏传都会静默产生无来源的审计记录。

contextvars 是标准库能力，分工因此变成：接口层负责绑定，应用层负责读取，
两层都不引入新耦合；未绑定（CLI 形态、后台任务）时读到空上下文，
审计的 IP/UA 落 ``NULL`` 而不是崩溃。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator

logger = logging.getLogger(__name__)

MAX_IP_CHARS = 64
"""IP 字段的字符上限。

WHY 设上限：``X-Forwarded-For`` 由客户端可控，超长串会污染审计表并放大
存储；IPv6 带映射与区域标识的最长形态也在 64 字符以内。
"""

MAX_USER_AGENT_CHARS = 512
"""User-Agent 的字符上限。

WHY 截断而非拒收：UA 只是审计的辅助定位信息，截断不影响可检索性，
而拒收会让一次正常的业务请求因为头过大而失败。
"""


@dataclass(frozen=True)
class RequestContext:
    """一次请求中与审计相关的客户端信息。

    Attributes:
        ip: 客户端 IP；未知时为空串（落库为 ``NULL``）。
        user_agent: 客户端 User-Agent；未知时为空串。
    """

    ip: str = ""
    user_agent: str = ""


_EMPTY_CONTEXT = RequestContext()

_REQUEST_CONTEXT: ContextVar[RequestContext] = ContextVar(
    "agent_request_context", default=_EMPTY_CONTEXT
)


def _clamp(value: object, limit: int, field: str) -> str:
    """规整客户端提供的文本：非字符串视为缺失，超长部分截断。

    Args:
        value: 原始值，可能来自任意请求头。
        limit: 字符上限。
        field: 字段名，仅用于日志。

    Returns:
        去空白后的字符串；``None`` 或非字符串返回空串。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        logger.warning("请求上下文字段 %s 不是字符串（%s），已忽略", field, type(value).__name__)
        return ""
    text = value.strip()
    if len(text) > limit:
        logger.debug("请求上下文字段 %s 超长已截断：%d -> %d", field, len(text), limit)
        return text[:limit]
    return text


def current_request_context() -> RequestContext:
    """返回当前协程绑定的请求上下文；未绑定时返回空上下文。

    Returns:
        请求上下文；永不返回 ``None``，调用方无需判空。
    """
    return _REQUEST_CONTEXT.get()


def bind_request_context(*, ip: str | None = None, user_agent: str | None = None) -> Token:
    """为当前协程绑定请求上下文。

    WHY 返回 ``Token`` 而不是提供 ``clear()``：``Token`` 只能由绑定者持有，
    重置时按栈回滚到绑定前的值，嵌套绑定（例如中间件内再起子任务）不会
    误把外层上下文清掉。

    Args:
        ip: 客户端 IP；``None`` 或空串视为未知。
        user_agent: 客户端 User-Agent；``None`` 或空串视为未知。

    Returns:
        用于 ``reset_request_context`` 的令牌。
    """
    context = RequestContext(
        ip=_clamp(ip, MAX_IP_CHARS, "ip"),
        user_agent=_clamp(user_agent, MAX_USER_AGENT_CHARS, "user_agent"),
    )
    return _REQUEST_CONTEXT.set(context)


def reset_request_context(token: Token) -> None:
    """按令牌回滚请求上下文。

    Args:
        token: ``bind_request_context`` 的返回值。

    Raises:
        ValueError: ``token`` 为 ``None``。
    """
    if token is None:
        raise ValueError("token 不能为 None")
    _REQUEST_CONTEXT.reset(token)


@contextmanager
def request_context(**kwargs: str | None) -> Iterator[RequestContext]:
    """以上下文管理器的方式绑定请求上下文，退出时自动回滚。

    WHY 提供这层语法糖：中间件与测试都要「绑定 → 做事 → 必然回滚」，
    手写 try/finally 一旦漏掉 finally，上下文会泄漏到同一个事件循环里
    后续被复用的协程上，表现为审计记录串到别人的 IP 上。

    Args:
        **kwargs: 与 ``bind_request_context`` 相同的 ``ip`` / ``user_agent``。

    Yields:
        已绑定的请求上下文。
    """
    token = bind_request_context(**kwargs)  # type: ignore[arg-type]
    try:
        yield current_request_context()
    finally:
        reset_request_context(token)


def audit_client_info() -> tuple[str | None, str | None]:
    """返回可直接写入审计记录的 ``(ip, user_agent)``。

    WHY 空串转 ``None``：审计列的语义是「未知」而非「空字符串」，
    落 ``NULL`` 才能让 ``WHERE ip IS NULL`` 这类查询成立。

    Returns:
        二元组；未知字段为 ``None``。
    """
    context = current_request_context()
    return (context.ip or None, context.user_agent or None)


__all__ = [
    "MAX_IP_CHARS",
    "MAX_USER_AGENT_CHARS",
    "RequestContext",
    "audit_client_info",
    "bind_request_context",
    "current_request_context",
    "request_context",
    "reset_request_context",
]
