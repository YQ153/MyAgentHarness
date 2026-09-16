"""登出与身份查询路由。

职责边界：管理本地会话的结束与主体信息的对外呈现。OIDC 登录往返在 ``flow`` 模块。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from application.principal import PERMISSIONS, Principal
from config import AppConfig
from interfaces.web.auth.audit import log_auth_event
from interfaces.web.auth.constants import STATE_COOKIE, VERIFIER_COOKIE
from interfaces.web.auth.deps import get_principal
from interfaces.web.auth.oidc import get_oidc_discovery
from interfaces.web.auth.session import (
    clear_ephemeral_cookie,
    clear_session_cookie,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """清除本地会话并返回首页；OIDC 模式下同时通知 IdP 结束会话。"""
    config: AppConfig = request.app.state.config
    principal = await get_principal(request)
    actor_id = principal.user_id if principal else "anonymous"

    await log_auth_event(
        request,
        event_type="logout",
        actor_id=actor_id,
        action="logout",
        outcome="success",
    )

    # WHY RP-initiated logout：只清本地 Cookie 时 IdP 仍认为用户已登录，
    # 前端一请求 /auth/login 就会被自动回调并重新建立会话，表现为「退出即登录」。
    redirect_url = "/"
    if config.auth_mode == "oidc":
        try:
            http: httpx.AsyncClient = request.app.state.http_client
            discovery = await get_oidc_discovery(config.oidc_issuer, http)
            end_session = discovery.get("end_session_endpoint")
            if end_session:
                post_logout = str(request.base_url).rstrip("/") + "/"
                redirect_url = (
                    f"{end_session}?{urlencode({'post_logout_redirect_uri': post_logout})}"
                )
        except httpx.HTTPError:
            # WHY 降级而非报错：IdP 不可达时本地会话已清除，登出目的已经达成，
            # 不应因远端问题把用户卡在错误页上。
            logger.exception("获取 end_session_endpoint 失败，fallback 到本地登出")

    response = RedirectResponse(redirect_url)
    clear_session_cookie(response, config)
    clear_ephemeral_cookie(response, STATE_COOKIE, config)
    clear_ephemeral_cookie(response, VERIFIER_COOKIE, config)
    return response


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
        "login_url": "/auth/login" if config.auth_mode == "oidc" else None,
        "logout_url": "/auth/logout",
        "me_url": "/auth/me",
    }
