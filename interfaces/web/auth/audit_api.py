"""审计日志查询端点。

WHY 与 ``audit``（写入辅助）分开：写入是认证流程的内部动作，查询是面向管理员的
对外接口，两者变更原因与访问控制不同。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from application.ports import AuditSink
from application.principal import Principal
from interfaces.web.auth.deps import require_permission

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
        HTTPException: 401/403 由 ``require_permission`` 抛出。
    """
    audit_store: AuditSink = request.app.state.audit_store
    return await audit_store.list(
        actor_id=actor_id,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )
