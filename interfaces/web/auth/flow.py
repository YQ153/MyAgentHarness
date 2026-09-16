"""OIDC Authorization Code + PKCE 的登录与回调路由。

职责边界：只覆盖「跳转到 IdP → 带回授权码 → 换取并校验 IdP 令牌 → 建立本地会话」
这一段往返。登出与身份查询在 ``identity`` 模块。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from application.principal import ROLE_PERMISSIONS, Principal
from config import AppConfig
from interfaces.web.auth.audit import log_auth_event
from interfaces.web.auth.constants import NEXT_COOKIE, STATE_COOKIE, VERIFIER_COOKIE
from interfaces.web.auth.deps import enforce_rate_limit
from interfaces.web.auth.oidc import (
    exchange_code,
    get_jwks,
    get_oidc_discovery,
    verify_id_token,
)
from interfaces.web.auth.session import (
    clear_ephemeral_cookie,
    get_ephemeral_cookie,
    set_ephemeral_cookie,
    set_session_cookie,
)
from interfaces.web.auth.utils import safe_next

logger = logging.getLogger(__name__)

router = APIRouter()


def _role_from_claims(claims: dict[str, Any]) -> str:
    """从 IdP claim 中解析角色，无显式声明时默认为 member。

    WHY 默认最小权限：claim 缺失可能来自 IdP 侧配置遗漏，此时若默认给高权限，
    会把一次配置错误直接放大成越权漏洞。
    """
    role = claims.get("harness_role") or claims.get("role")
    if role in ROLE_PERMISSIONS:
        return role
    return "member"


def _scopes_from_claims(claims: dict[str, Any]) -> frozenset[str]:
    """从 ``scope`` claim 解析 OAuth scope 集合。"""
    scope_str = claims.get("scope", "")
    if isinstance(scope_str, str):
        return frozenset(scope_str.split())
    return frozenset()


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """重定向到 IdP 登录页（仅 OIDC 模式）。

    Raises:
        HTTPException: 400 非 OIDC 模式；503 IdP 未提供授权端点。
    """
    enforce_rate_limit(request)
    config: AppConfig = request.app.state.config
    if config.auth_mode != "oidc":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前认证模式不是 OIDC",
        )

    discovery = await get_oidc_discovery(
        config.oidc_issuer, request.app.state.http_client
    )
    auth_endpoint = discovery.get("authorization_endpoint")
    if not auth_endpoint:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IdP 未提供 authorization_endpoint",
        )

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")

    # 记录登录后的跳转地址，供 device flow 等场景在登录后回到原页面
    next_url = safe_next(request.query_params.get("next"))

    params = {
        "response_type": "code",
        "client_id": config.oidc_client_id,
        "redirect_uri": config.oidc_redirect_uri,
        "scope": config.oidc_scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{auth_endpoint}?{urlencode(params)}"

    response = RedirectResponse(url)
    set_ephemeral_cookie(response, STATE_COOKIE, state, config)
    set_ephemeral_cookie(response, VERIFIER_COOKIE, verifier, config)
    set_ephemeral_cookie(response, NEXT_COOKIE, next_url, config)
    return response


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    """处理 IdP 回调并建立本地会话。

    Raises:
        HTTPException: 400 缺少参数或非 OIDC 模式；401 state 不匹配、换取令牌失败或 ID Token 无效。
    """
    enforce_rate_limit(request)
    config: AppConfig = request.app.state.config
    if config.auth_mode != "oidc":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前认证模式不是 OIDC",
        )

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少 code 或 state",
        )

    stored_state = get_ephemeral_cookie(request, STATE_COOKIE)
    verifier = get_ephemeral_cookie(request, VERIFIER_COOKIE)
    if not stored_state or not secrets.compare_digest(state, stored_state):
        await log_auth_event(
            request,
            event_type="login_failure",
            actor_id="unknown",
            action="oidc_callback",
            outcome="failure",
            details={"reason": "state_mismatch"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC state 不匹配或已过期",
        )

    http: httpx.AsyncClient = request.app.state.http_client
    try:
        token_response = await exchange_code(code, verifier, config=config, http=http)
    except HTTPException as exc:
        await log_auth_event(
            request,
            event_type="login_failure",
            actor_id="unknown",
            action="oidc_callback",
            outcome="failure",
            details={"reason": "token_exchange_failed", "detail": exc.detail},
        )
        raise

    id_token = token_response.get("id_token")
    if not id_token:
        await log_auth_event(
            request,
            event_type="login_failure",
            actor_id="unknown",
            action="oidc_callback",
            outcome="failure",
            details={"reason": "missing_id_token"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="IdP 未返回 id_token",
        )

    discovery = await get_oidc_discovery(config.oidc_issuer, http)
    jwks = await get_jwks(discovery["jwks_uri"], http)
    try:
        # WHY 用 discovery 返回的 issuer：配置中的 issuer 可能因末尾斜杠、
        # host 大小写、http/https 与实际 token 中的 iss 不一致，而 discovery
        # 端点是 IdP 自我声明的权威 issuer，按 OIDC 规范应以此为准。
        claims = verify_id_token(
            id_token,
            issuer=discovery["issuer"],
            client_id=config.oidc_client_id,
            jwks=jwks,
        )
    except HTTPException as exc:
        await log_auth_event(
            request,
            event_type="login_failure",
            actor_id="unknown",
            action="oidc_callback",
            outcome="failure",
            details={"reason": "id_token_invalid", "detail": exc.detail},
        )
        raise

    principal = Principal(
        user_id=claims["sub"],
        display_name=claims.get("name", ""),
        email=claims.get("email", ""),
        role=_role_from_claims(claims),
        scopes=_scopes_from_claims(claims),
        auth_method="oidc",
    )

    session = {
        "user_id": principal.user_id,
        "display_name": principal.display_name,
        "email": principal.email,
        "role": principal.role,
        "scopes": list(principal.scopes),
        "auth_method": principal.auth_method,
    }

    await log_auth_event(
        request,
        event_type="login_success",
        actor_id=principal.user_id,
        action="oidc_callback",
        outcome="success",
        details={"role": principal.role, "email": principal.email},
    )

    next_url = get_ephemeral_cookie(request, NEXT_COOKIE) or "/"
    response = RedirectResponse(safe_next(next_url))
    clear_ephemeral_cookie(response, STATE_COOKIE, config)
    clear_ephemeral_cookie(response, VERIFIER_COOKIE, config)
    clear_ephemeral_cookie(response, NEXT_COOKIE, config)
    set_session_cookie(response, session, config)
    return response
