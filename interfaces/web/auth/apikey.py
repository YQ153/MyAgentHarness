"""API Key 管理端点。

职责边界：只负责 API Key 的增删查，鉴权通过 ``require_permission`` 统一施加。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status

from application.ports import APIKeyRepository
from application.principal import Principal
from interfaces.web.auth.audit import log_auth_event
from interfaces.web.auth.deps import require_permission

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api-keys")
async def create_api_key(
    request: Request,
    role: str = Form("member"),
    scopes: str = Form(""),
    description: str = Form(""),
    expires_at: str | None = Form(None),
    principal: Principal = Depends(require_permission("apikey:manage")),
) -> dict[str, Any]:
    """创建一条新的 API Key。

    Returns:
        含 ``key_id`` 与一次性明文 ``key`` 的字典。

    Raises:
        HTTPException: 401/403 由 ``require_permission`` 抛出。
    """
    store: APIKeyRepository = request.app.state.api_key_store
    scope_list = [s.strip() for s in scopes.split() if s.strip()]

    result = await store.create(
        role=role,
        scopes=scope_list,
        description=description,
        expires_at=expires_at,
    )
    await log_auth_event(
        request,
        event_type="apikey_created",
        actor_id=principal.user_id,
        action="create_api_key",
        outcome="success",
        details={"key_id": result["key_id"], "role": role, "scopes": scope_list},
    )
    return result


@router.get("/api-keys")
async def list_api_keys(
    request: Request,
    include_revoked: bool = False,
    principal: Principal = Depends(require_permission("apikey:manage")),
) -> list[dict[str, Any]]:
    """列出 API Key。"""
    store: APIKeyRepository = request.app.state.api_key_store
    return await store.list(include_revoked=include_revoked)


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    request: Request,
    key_id: str,
    principal: Principal = Depends(require_permission("apikey:manage")),
) -> dict[str, Any]:
    """吊销指定 API Key。

    Raises:
        HTTPException: 404 Key 不存在；401/403 由 ``require_permission`` 抛出。
    """
    store: APIKeyRepository = request.app.state.api_key_store
    revoked = await store.revoke(key_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API Key 不存在",
        )
    await log_auth_event(
        request,
        event_type="apikey_revoked",
        actor_id=principal.user_id,
        action="revoke_api_key",
        outcome="success",
        details={"key_id": key_id},
    )
    return {"key_id": key_id, "revoked": True}
