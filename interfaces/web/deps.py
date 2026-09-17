"""Web 层共享的依赖项。

WHY 从 ``routes`` 中分出来：业务路由与运维路由都需要「从应用状态取服务」，
各自留一份私有实现会让两处的缺失判定（状态码与文案）逐渐漂移。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status


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


__all__ = ["require_state"]
