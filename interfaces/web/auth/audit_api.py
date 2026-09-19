"""审计日志查询端点。

WHY 与 ``audit``（写入辅助）分开：写入是认证流程的内部动作，查询是面向管理员的
对外接口，两者变更原因与访问控制不同。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from application.principal import Principal
from interfaces.web.auth.deps import require_permission
from interfaces.web.deps import require_state

router = APIRouter()


@router.get("/audit")
async def list_audit(
    request: Request,
    actor_id: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    principal: Principal = Depends(require_permission("audit:read")),
) -> list[dict[str, Any]]:
    """读取审计日志（仅管理员）。

    Raises:
        HTTPException: 401/403 由 ``require_permission`` 抛出；503 表示审计存储未装配。
    """
    # WHY 走 require_state 而不是直接取属性：直接取会在「lifespan 漏铺一项」时抛
    # AttributeError，由框架兜成 500 + 一屏栈——运维看到的是「服务端有 bug」，而这
    # 其实是「依赖没装配」。503 才是这条事实的准确表达，也让探活系统能正确摘除实例。
    audit_store = require_state(request, "audit_store", "审计日志存储")
    return await audit_store.list(
        actor_id=actor_id,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )
