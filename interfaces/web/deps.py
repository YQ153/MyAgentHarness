"""Web 层共享的依赖项与请求元数据工具。

WHY 从 ``routes`` 中分出来：业务路由与运维路由都需要「从应用状态取服务」，
各自留一份私有实现会让两处的缺失判定（状态码与文案）逐渐漂移。

请求元数据（``client_ip`` / ``user_agent``）也放在这里：它们被审计上下文中间件
与审计写入路径共用，放在任一调用方里都会让另一侧去导入一个与它无关的模块。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request, status

from application.errors import (
    NotFoundError,
    SessionRootLockedError,
    SessionRootNotReadyError,
    SessionRootUnavailableError,
)

#: 解析会话根时属于「状态不允许」的那几种失败：都映射为 409。
#:
#: WHY 收成一个元组：它们对调用方的含义相同（重试本请求无用，得先改状态或改参数），
#: 而漏掉其中一个的表现正是本次修的那个 bug——``SessionRootUnavailableError`` 漏在外面时
#: 会掉进 500，用户看到一个与他的操作毫无关系的服务端故障。
_ROOT_CONFLICT_ERRORS = (
    SessionRootLockedError,
    SessionRootNotReadyError,
    SessionRootUnavailableError,
)

logger = logging.getLogger(__name__)


def client_ip(request: Request) -> str:
    """获取客户端 IP，优先读取反向代理透传头。

    WHY 优先读 ``X-Forwarded-For``：应用部署在反向代理后方时，
    ``request.client.host`` 只会是代理自身地址，无法用于审计定位。
    注意该头可被客户端伪造，因此只用于「审计与排查」这类可容忍偏差的场景。

    Args:
        request: 当前请求。

    Returns:
        客户端 IP；无法确定时返回 ``"unknown"``。
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-Ip")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return "unknown"


def user_agent(request: Request) -> str:
    """提取 User-Agent，缺失时返回空串。"""
    return request.headers.get("user-agent", "") or ""


def require_state(request: Request, attr: str, label: str) -> Any:
    """从应用状态取服务，缺失时返回 503。

    WHY 用 503 而不是 500：服务未装配意味着进程尚未完成启动或正在关闭，
    这是「暂时不可用」而非「服务端有 bug」；返回 503 能让探活系统正确地
    把该实例摘除，而不是在错误率里记一笔 5xx。

    Args:
        request: 当前请求。
        attr: ``app.state`` 上的属性名。
        label: 人类可读的服务名，用于错误文案。

    Returns:
        已装配的服务实例。

    Raises:
        HTTPException: 503，服务未初始化。
    """
    service = getattr(request.app.state, attr, None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{label}未初始化",
        )
    return service


def get_session_registry(request: Request) -> Any:
    """取出会话级工作区注册表。

    WHY 面板类端点也要走它：工作区在会话级可选之后，「当前工作区」不再是一个进程级
    常量。附件、技能视图、知识库索引与文件面板都按会话各有一份，直接用全局那一个会让
    B 会话的面板显示 A 工作区的文件——而两者都不会报错。

    Returns:
        已装配的 ``SessionRegistry``。

    Raises:
        HTTPException: 503，注册表未初始化。
    """
    return require_state(request, "workspaces", "会话根注册表")


async def resolve_scoped_services(
    request: Request,
    *,
    thread_id: str | None = None,
    requested: str | None = None,
) -> Any:
    """按会话解析出会话根服务集合，并把解析失败翻译成 HTTP 语义。

    WHY 集中在一个函数里：文件面板、附件、技能、知识库四个面板都要做同一件事，
    各写一遍必然出现「这处映射成 409、那处漏成 500」的漂移——而漂移的方向正是
    「本该给出明确说法的请求被当成服务端故障」。

    WHY 一直带 ``allow_missing``：面板可以在会话**尚未发出首条消息**时被打开，
    那时它在库里还不存在，而它将要使用的根正是 ``requested``。

    Args:
        request: 当前请求。
        thread_id: 会话 ID；``None`` 表示与具体会话无关（如草稿态面板）。
        requested: 请求里给出的工作空间；``None`` 表示「该会话已锁定的那个」。

    Returns:
        该会话生效的会话根服务集合。

    Raises:
        HTTPException: 400（路径不存在或不是目录）、404（会话不存在）、409（根已锁定、
            还没确定，或不可用）。
    """
    registry = get_session_registry(request)
    try:
        return await registry.services_for(
            requested=requested, thread_id=thread_id, allow_missing=True
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except _ROOT_CONFLICT_ERRORS as exc:
        # 都是 409：状态冲突（已锁定）、状态未就绪（还没有根）、状态不可用（目录不见了）
        # ——客户端的正确处理都不是「重试同一个请求」，而是改参数、先发出第一条消息，
        # 或把那个目录恢复回来。文案里已经写明该做哪一样。
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        # 路径不存在、不是目录等取值问题：改路径就能过。
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


async def describe_session_root(
    request: Request,
    *,
    thread_id: str | None = None,
    requested: str | None = None,
) -> Any:
    """取当前会话的文件根信息，并把解析失败翻译成与 :func:`resolve_scoped_services` 一致的语义。

    WHY 也放在这里：它和「取服务」是同一类操作（都要解析会话根），映射规则分两处写迟早
    漂开——而漂开的表现是同一种失败在两个端点上给出不同的状态码。

    Raises:
        HTTPException: 400（路径不存在）、404（会话不存在）、409（已锁定 / 还没有根 /
            根不可用）、500（其他未预期的失败）。
    """
    registry = get_session_registry(request)
    try:
        return await registry.describe(thread_id=thread_id, requested=requested)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except _ROOT_CONFLICT_ERRORS as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("读取会话根信息失败：thread=%s", thread_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


__all__ = [
    "client_ip",
    "describe_session_root",
    "get_session_registry",
    "require_state",
    "resolve_scoped_services",
    "user_agent",
]
