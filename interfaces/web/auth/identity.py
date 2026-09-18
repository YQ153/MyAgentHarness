"""登出与身份查询路由。

职责边界：登出只记审计并送回首页（凭据由调用方持有，服务端没有可清除的会话状态）；
身份查询把当前 ``Principal`` 呈现给前端。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from application.principal import PERMISSIONS, Principal
from config import AppConfig
from interfaces.web.auth.audit import log_auth_event
from interfaces.web.auth.deps import get_principal

router = APIRouter()


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """记录登出并返回首页。

    WHY 保留这个端点而不在 apikey 模式下删掉它：凭据由调用方持有（客户端存着 API Key），
    服务端没有可清除的会话状态，"登出" 在这里只剩「记一笔审计 + 把用户送回首页」。
    删掉它反而会让前端与 CLI 的既有调用路径失效。
    """
    principal = await get_principal(request)
    actor_id = principal.user_id if principal else "anonymous"

    await log_auth_event(
        request,
        event_type="logout",
        actor_id=actor_id,
        action="logout",
        outcome="success",
    )

    return RedirectResponse("/")


@router.get("/me")
async def me(principal: Principal | None = Depends(get_principal)) -> dict[str, Any]:
    """返回当前主体信息，供前端判断登录态。

    Raises:
        HTTPException: 401 未认证。
    """
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未认证")
    return {
        "user_id": principal.user_id,
        "display_name": principal.display_name,
        "email": principal.email,
        "role": principal.role,
        "scopes": sorted(principal.scopes),
        "auth_method": principal.auth_method,
        "permissions": sorted(
            {p for p in PERMISSIONS if principal.has_permission(p)}
        ),
    }


@router.get("/config")
async def auth_config(request: Request) -> dict[str, Any]:
    """返回前端所需的认证配置。"""
    config: AppConfig = request.app.state.config
    return {
        "auth_mode": config.auth_mode,
        "logout_url": "/auth/logout",
        "me_url": "/auth/me",
    }
