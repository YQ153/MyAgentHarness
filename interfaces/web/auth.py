"""Web 端认证与授权基础设施。

职责边界：只处理 HTTP 层面的身份提取、会话管理、OIDC 回调与 API Key 管理，
不持有业务权限规则（规则在 ``application.principal``）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
import jwt
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from application.principal import ANONYMOUS_PRINCIPAL, PERMISSIONS, Principal, ROLE_PERMISSIONS
from application.rate_limiter import RateLimiter
from config import AppConfig
from runtime.api_key_store import APIKeyStore
from runtime.audit_store import AuditStore
from runtime.device_flow_store import DeviceFlowStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_VERIFIER_COOKIE = "harness_oidc_verifier"
_STATE_COOKIE = "harness_oidc_state"
_NEXT_COOKIE = "harness_oidc_next"


def _client_ip(request: Request) -> str:
    """获取客户端 IP，优先读取反向代理透传头。"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-Ip")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return "unknown"


def _user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "") or ""


def _safe_next(next_url: str | None) -> str:
    """防止开放重定向：只允许以 ``/`` 开头的同源路径。"""
    if not next_url:
        return "/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


def _get_bearer_token(request: Request) -> str | None:
    """从 Authorization: Bearer 头中提取 token。"""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip() or None


# ------------------------------------------------------------------ 会话工具


def _session_serializer(config: AppConfig) -> URLSafeTimedSerializer:
    """返回本地会话签名器；密钥不足时会直接炸。"""
    secret = config.auth_session_secret
    if not secret or len(secret) < 32:
        raise ValueError("auth_session_secret 至少需要 32 字节")
    return URLSafeTimedSerializer(secret)


def get_session(request: Request) -> dict[str, Any] | None:
    """从签名 Cookie 中读取本地会话数据。"""
    config: AppConfig = request.app.state.config
    raw = request.cookies.get(config.auth_cookie_name)
    if not raw:
        return None
    try:
        return _session_serializer(config).loads(
            raw, max_age=config.auth_session_max_age_seconds
        )
    except (BadSignature, SignatureExpired):
        return None


def set_session_cookie(response: Response, data: dict[str, Any], config: AppConfig) -> None:
    """把会话数据签名后写入 Cookie。"""
    signed = _session_serializer(config).dumps(data)
    # WHY Secure 默认 False：本地 127.0.0.1 开发时浏览器会拒绝 Secure Cookie，
    # 因此把开关交给配置；生产部署必须设为 True 并配合 HTTPS / 反向代理。
    response.set_cookie(
        key=config.auth_cookie_name,
        value=signed,
        max_age=config.auth_session_max_age_seconds,
        httponly=True,
        secure=config.auth_cookie_secure,
        samesite=config.auth_cookie_samesite,
        path="/",
    )


def clear_session_cookie(response: Response, config: AppConfig) -> None:
    """清除会话 Cookie。

    WHY 不用 ``response.delete_cookie``：Starlette 的 ``delete_cookie`` 不支持
    设置 Secure / SameSite，生产环境删除 Secure Cookie 时必须带上相同属性，
    否则浏览器不会匹配并清除。
    """
    response.set_cookie(
        key=config.auth_cookie_name,
        value="",
        max_age=0,
        expires=0,
        httponly=True,
        secure=config.auth_cookie_secure,
        samesite=config.auth_cookie_samesite,
        path="/",
    )


def _set_ephemeral_cookie(
    response: Response,
    name: str,
    value: str,
    max_age: int = 600,
    *,
    config: AppConfig,
) -> None:
    """设置一次性签名 Cookie（如 PKCE verifier / state）。"""
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=True,
        secure=config.auth_cookie_secure,
        samesite=config.auth_cookie_samesite,
        path="/auth/callback",
    )


def _get_ephemeral_cookie(request: Request, name: str) -> str | None:
    return request.cookies.get(name)


def _clear_ephemeral_cookie(response: Response, name: str, config: AppConfig) -> None:
    """清除一次性 OIDC Cookie。"""
    response.set_cookie(
        key=name,
        value="",
        max_age=0,
        expires=0,
        httponly=True,
        secure=config.auth_cookie_secure,
        samesite=config.auth_cookie_samesite,
        path="/auth/callback",
    )


async def _audit_auth(
    request: Request,
    *,
    event_type: str,
    actor_id: str,
    action: str | None = None,
    outcome: str,
    details: dict[str, Any] | None = None,
) -> None:
    """记录认证相关审计事件。"""
    audit_store: AuditStore | None = getattr(request.app.state, "audit_store", None)
    if audit_store is None:
        return
    await audit_store.log(
        event_type=event_type,
        actor_id=actor_id,
        action=action,
        outcome=outcome,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        details=details,
    )


def _check_rate_limit(request: Request) -> None:
    """认证端点限流检查；超限返回 429。"""
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    ip = _client_ip(request)
    if not limiter.is_allowed(ip):
        logger.warning("认证端点触发限流：ip=%s", ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试",
        )


# ------------------------------------------------------------------ Principal 提取


async def get_principal(request: Request) -> Principal | None:
    """提取当前请求的认证主体。

    - ``disabled`` 模式：返回匿名管理员主体，保持原有行为。
    - ``apikey`` 模式：从请求头读取 API Key，失败时 fallback 到会话 Cookie。
    - ``oidc`` 模式：优先读取本地会话 Cookie。

    Returns:
        Principal；未认证时返回 ``None``。
    """
    config: AppConfig = request.app.state.config

    if config.auth_mode == "disabled":
        return ANONYMOUS_PRINCIPAL

    if config.auth_mode == "apikey":
        api_key = request.headers.get(config.auth_api_key_header) or _get_bearer_token(request)
        if api_key:
            return await _validate_api_key(request, api_key)
        session = get_session(request)
        if session:
            return _principal_from_session(session)
        return None

    if config.auth_mode == "oidc":
        session = get_session(request)
        if session:
            return _principal_from_session(session)
        return None

    return None


async def _validate_api_key(request: Request, api_key: str) -> Principal | None:
    """校验 API Key 并返回对应主体。"""
    store: APIKeyStore | None = getattr(request.app.state, "api_key_store", None)
    config: AppConfig = request.app.state.config

    # WHY 兜底 dev key：最小可用与单节点场景下保留环境变量快速入口，
    # 生产环境应把 dev key 置空，强制走数据库 key store。
    if config.auth_api_key_dev and secrets.compare_digest(api_key, config.auth_api_key_dev):
        await _audit_auth(
            request,
            event_type="apikey_auth_success",
            actor_id="apikey:dev",
            action="validate",
            outcome="success",
            details={"source": "env_dev_key"},
        )
        return Principal(
            user_id="apikey:dev",
            display_name="dev",
            role="admin",
            scopes=frozenset(ROLE_PERMISSIONS["admin"]),
            auth_method="apikey",
        )

    if store is None:
        await _audit_auth(
            request,
            event_type="apikey_auth_failure",
            actor_id="unknown",
            action="validate",
            outcome="failure",
            details={"reason": "store_unavailable"},
        )
        return None

    record = await store.validate(api_key)
    if record is None:
        await _audit_auth(
            request,
            event_type="apikey_auth_failure",
            actor_id="unknown",
            action="validate",
            outcome="failure",
            details={"reason": "invalid_or_revoked"},
        )
        return None

    actor_id = f"apikey:{record['key_id']}"
    await _audit_auth(
        request,
        event_type="apikey_auth_success",
        actor_id=actor_id,
        action="validate",
        outcome="success",
        details={"key_id": record["key_id"], "role": record["role"]},
    )
    return Principal(
        user_id=actor_id,
        display_name=f"API Key {record.get('key_prefix', '')}...",
        role=record["role"],
        scopes=frozenset((record.get("scopes") or "").split()),
        auth_method="apikey",
    )


def _principal_from_session(session: dict[str, Any]) -> Principal:
    return Principal(
        user_id=session["user_id"],
        display_name=session.get("display_name", ""),
        email=session.get("email", ""),
        role=session.get("role", "member"),
        scopes=frozenset(session.get("scopes", [])),
        auth_method=session.get("auth_method", "cookie"),
    )


def require_permission(permission: str):
    """路由层权限校验依赖项工厂。"""

    async def checker(
        request: Request,
        principal: Principal | None = Depends(get_principal),
    ) -> Principal:
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="未认证",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not principal.has_permission(permission):
            await _audit_auth(
                request,
                event_type="permission_denied",
                actor_id=principal.user_id,
                action=permission,
                outcome="failure",
                details={"permission": permission},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"缺少权限：{permission}",
            )
        return principal

    return checker


# ------------------------------------------------------------------ OIDC 流程


_oidc_cache: dict[str, Any] = {}


def _discovery_url(issuer: str) -> str:
    """从 issuer 构造 discovery URL，不修改 issuer 本身。"""
    return urljoin(issuer.rstrip("/") + "/", ".well-known/openid-configuration")


async def _get_oidc_discovery(issuer: str, http: httpx.AsyncClient) -> dict[str, Any]:
    """拉取并缓存 OIDC Discovery 配置。"""
    cache_key = f"discovery:{issuer}"
    if cache_key in _oidc_cache:
        return _oidc_cache[cache_key]
    discovery_url = _discovery_url(issuer)
    try:
        resp = await http.get(discovery_url, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("OIDC Discovery 失败：%s", discovery_url)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IdP 不可用",
        ) from exc
    data = resp.json()
    _oidc_cache[cache_key] = data
    return data


async def _get_jwks(jwks_uri: str, http: httpx.AsyncClient) -> dict[str, Any]:
    """拉取并缓存 JWKS。"""
    cache_key = f"jwks:{jwks_uri}"
    if cache_key in _oidc_cache:
        return _oidc_cache[cache_key]
    try:
        resp = await http.get(jwks_uri, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("JWKS 拉取失败：%s", jwks_uri)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IdP 证书不可用",
        ) from exc
    data = resp.json()
    _oidc_cache[cache_key] = data
    return data


def _verify_id_token(
    id_token: str,
    *,
    issuer: str,
    client_id: str,
    jwks: dict[str, Any],
    nonce: str | None = None,
) -> dict[str, Any]:
    """用 JWKS 验证 id_token 签名并校验关键 claim。"""
    unverified = jwt.get_unverified_header(id_token)
    kid = unverified.get("kid")
    key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="IdP 签名密钥不匹配",
        )
    try:
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无法解析 IdP 公钥",
        ) from exc

    try:
        payload = jwt.decode(
            id_token,
            public_key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=issuer,
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ID Token 已过期",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"ID Token 无效：{exc}",
        ) from exc

    if nonce is not None and payload.get("nonce") != nonce:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC nonce 不匹配",
        )
    return payload


async def _exchange_code(
    code: str,
    verifier: str | None,
    *,
    config: AppConfig,
    http: httpx.AsyncClient,
) -> dict[str, Any]:
    """用授权码换取 token。"""
    discovery = await _get_oidc_discovery(config.oidc_issuer, http)
    token_endpoint = discovery.get("token_endpoint")
    if not token_endpoint:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IdP 未提供 token_endpoint",
        )
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.oidc_redirect_uri,
        "client_id": config.oidc_client_id,
        "client_secret": config.oidc_client_secret,
    }
    if verifier:
        data["code_verifier"] = verifier
    try:
        resp = await http.post(token_endpoint, data=data, timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("OIDC token 交换失败")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无法用授权码换取令牌",
        ) from exc
    return resp.json()


def _role_from_claims(claims: dict[str, Any]) -> str:
    """从 IdP claim 中解析角色，无显式声明时默认为 member。"""
    role = claims.get("harness_role") or claims.get("role")
    if role in ROLE_PERMISSIONS:
        return role
    return "member"


def _scopes_from_claims(claims: dict[str, Any]) -> frozenset[str]:
    scope_str = claims.get("scope", "")
    if isinstance(scope_str, str):
        return frozenset(scope_str.split())
    return frozenset()


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """重定向到 IdP 登录页（仅 OIDC 模式）。"""
    _check_rate_limit(request)
    config: AppConfig = request.app.state.config
    if config.auth_mode != "oidc":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前认证模式不是 OIDC",
        )

    discovery = await _get_oidc_discovery(
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

    # 记录登录后的跳转地址，用于 device flow 等场景
    next_url = _safe_next(request.query_params.get("next"))

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
    _set_ephemeral_cookie(response, _STATE_COOKIE, state, config=config)
    _set_ephemeral_cookie(response, _VERIFIER_COOKIE, verifier, config=config)
    _set_ephemeral_cookie(response, _NEXT_COOKIE, next_url, config=config)
    return response


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    """处理 IdP 回调并建立本地会话。"""
    _check_rate_limit(request)
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

    stored_state = _get_ephemeral_cookie(request, _STATE_COOKIE)
    verifier = _get_ephemeral_cookie(request, _VERIFIER_COOKIE)
    if not stored_state or not secrets.compare_digest(state, stored_state):
        await _audit_auth(
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
        token_response = await _exchange_code(code, verifier, config=config, http=http)
    except HTTPException as exc:
        await _audit_auth(
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
        await _audit_auth(
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

    discovery = await _get_oidc_discovery(config.oidc_issuer, http)
    jwks = await _get_jwks(discovery["jwks_uri"], http)
    try:
        # WHY 用 discovery 返回的 issuer：配置中的 issuer 可能因末尾斜杠、
        # host 大小写、http/https 与实际 token 中的 iss 不一致，而 discovery
        # 端点是 IdP 自我声明的权威 issuer，按 OIDC 规范应以此为准。
        claims = _verify_id_token(
            id_token,
            issuer=discovery["issuer"],
            client_id=config.oidc_client_id,
            jwks=jwks,
        )
    except HTTPException as exc:
        await _audit_auth(
            request,
            event_type="login_failure",
            actor_id="unknown",
            action="oidc_callback",
            outcome="failure",
            details={"reason": "id_token_invalid", "detail": exc.detail},
        )
        raise

    role = _role_from_claims(claims)
    principal = Principal(
        user_id=claims["sub"],
        display_name=claims.get("name", ""),
        email=claims.get("email", ""),
        role=role,
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

    await _audit_auth(
        request,
        event_type="login_success",
        actor_id=principal.user_id,
        action="oidc_callback",
        outcome="success",
        details={"role": principal.role, "email": principal.email},
    )

    next_url = _get_ephemeral_cookie(request, _NEXT_COOKIE) or "/"
    response = RedirectResponse(_safe_next(next_url))
    _clear_ephemeral_cookie(response, _STATE_COOKIE, config)
    _clear_ephemeral_cookie(response, _VERIFIER_COOKIE, config)
    _clear_ephemeral_cookie(response, _NEXT_COOKIE, config)
    set_session_cookie(response, session, config)
    return response


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """清除本地会话并返回首页；OIDC 模式下同时通知 IdP 结束会话。"""
    config: AppConfig = request.app.state.config
    principal = await get_principal(request)
    actor_id = principal.user_id if principal else "anonymous"

    await _audit_auth(
        request,
        event_type="logout",
        actor_id=actor_id,
        action="logout",
        outcome="success",
    )

    # WHY RP-initiated logout：只清本地 Cookie 时，IdP 仍认为用户已登录，
    # 前端一请求 /auth/login 就会被自动回调并重新建立会话，出现"退出即登录"。
    redirect_url = "/"
    if config.auth_mode == "oidc":
        try:
            http: httpx.AsyncClient = request.app.state.http_client
            discovery = await _get_oidc_discovery(config.oidc_issuer, http)
            end_session = discovery.get("end_session_endpoint")
            if end_session:
                post_logout = str(request.base_url).rstrip("/") + "/"
                redirect_url = f"{end_session}?{urlencode({'post_logout_redirect_uri': post_logout})}"
        except httpx.HTTPError:
            logger.exception("获取 end_session_endpoint 失败，fallback 到本地登出")

    response = RedirectResponse(redirect_url)
    clear_session_cookie(response, config)
    _clear_ephemeral_cookie(response, _STATE_COOKIE, config)
    _clear_ephemeral_cookie(response, _VERIFIER_COOKIE, config)
    return response


@router.get("/me")
async def me(principal: Principal | None = Depends(get_principal)) -> dict[str, Any]:
    """返回当前主体信息，供前端判断登录态。"""
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


# ------------------------------------------------------------------ API Key 管理


@router.post("/api-keys")
async def create_api_key(
    request: Request,
    role: str = Form("member"),
    scopes: str = Form(""),
    description: str = Form(""),
    expires_at: str | None = Form(None),
    principal: Principal = Depends(require_permission("apikey:manage")),
) -> dict[str, Any]:
    """创建一条新的 API Key。"""
    store: APIKeyStore = request.app.state.api_key_store
    scope_list = [s.strip() for s in scopes.split() if s.strip()]

    result = await store.create(
        role=role,
        scopes=scope_list,
        description=description,
        expires_at=expires_at,
    )
    await _audit_auth(
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
    store: APIKeyStore = request.app.state.api_key_store
    return await store.list(include_revoked=include_revoked)


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    request: Request,
    key_id: str,
    principal: Principal = Depends(require_permission("apikey:manage")),
) -> dict[str, Any]:
    """吊销指定 API Key。"""
    store: APIKeyStore = request.app.state.api_key_store
    revoked = await store.revoke(key_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API Key 不存在",
        )
    await _audit_auth(
        request,
        event_type="apikey_revoked",
        actor_id=principal.user_id,
        action="revoke_api_key",
        outcome="success",
        details={"key_id": key_id},
    )
    return {"key_id": key_id, "revoked": True}


# ------------------------------------------------------------------ 审计日志


@router.get("/audit")
async def list_audit(
    request: Request,
    actor_id: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    principal: Principal = Depends(require_permission("audit:read")),
) -> list[dict[str, Any]]:
    """读取审计日志（仅管理员）。"""
    audit_store: AuditStore = request.app.state.audit_store
    return await audit_store.list(
        actor_id=actor_id,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )


# ------------------------------------------------------------------ OIDC Device Flow（CLI 登录）


def _device_flow_expires_at(config: AppConfig) -> str:
    """按配置计算 device flow 过期时间。"""
    delta = timedelta(seconds=config.device_flow_expires_in_seconds)
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


def _device_flow_key_expires_at(config: AppConfig) -> str:
    """按配置计算 device flow 派生 API Key 的过期时间。"""
    delta = timedelta(days=config.device_flow_api_key_expires_in_days)
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


@router.post("/device/authorize")
async def device_authorize(request: Request) -> dict[str, Any]:
    """CLI 发起 device flow，返回 user_code 与轮询地址。"""
    _check_rate_limit(request)
    config: AppConfig = request.app.state.config
    if config.auth_mode != "oidc":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前认证模式不是 OIDC",
        )

    store: DeviceFlowStore = request.app.state.device_flow_store
    result = await store.create(expires_in_seconds=config.device_flow_expires_in_seconds)

    base_url = str(request.base_url).rstrip("/")
    verification_uri = f"{base_url}/auth/device/activate"

    await _audit_auth(
        request,
        event_type="device_flow_initiated",
        actor_id="cli",
        action="device_authorize",
        outcome="success",
        details={"user_code": result["user_code"]},
    )

    return {
        "device_code": result["device_code"],
        "user_code": result["user_code"],
        "verification_uri": verification_uri,
        "verification_uri_complete": f"{verification_uri}?user_code={result['user_code']}",
        "expires_in": result["expires_in"],
        "interval": config.device_flow_poll_interval_seconds,
    }


@router.get("/device/activate", response_class=HTMLResponse)
async def device_activate_page(request: Request) -> HTMLResponse:
    """浏览器端输入 user_code 并批准的页面。"""
    _check_rate_limit(request)
    config: AppConfig = request.app.state.config
    if config.auth_mode != "oidc":
        return HTMLResponse("<h1>当前认证模式不是 OIDC</h1>", status_code=400)

    principal = await get_principal(request)
    if principal is None:
        # 未登录则先走 OIDC，登录后带 next 跳转回来
        user_code = request.query_params.get("user_code", "")
        next_path = f"/auth/device/activate?user_code={user_code}"
        return RedirectResponse(f"/auth/login?next={next_path}")

    user_code = request.query_params.get("user_code", "")
    store: DeviceFlowStore = request.app.state.device_flow_store
    record = await store.get_by_user_code(user_code)
    if record is None or record.get("status") != "pending":
        return HTMLResponse(
            _render_activate_html(
                user_code=user_code,
                error="授权码无效或已过期，请在 CLI 重新发起。",
            ),
            status_code=400,
        )

    return HTMLResponse(
        _render_activate_html(
            user_code=user_code,
            principal=principal,
        )
    )


@router.post("/device/activate")
async def device_activate(
    request: Request,
    user_code: str = Form(...),
) -> HTMLResponse:
    """处理浏览器端的批准请求。"""
    _check_rate_limit(request)
    config: AppConfig = request.app.state.config
    if config.auth_mode != "oidc":
        return HTMLResponse("<h1>当前认证模式不是 OIDC</h1>", status_code=400)

    principal = await get_principal(request)
    if principal is None:
        return HTMLResponse(
            _render_activate_html(
                user_code=user_code,
                error="登录会话已过期，请刷新页面后重试。",
            ),
            status_code=401,
        )

    store: DeviceFlowStore = request.app.state.device_flow_store
    record = await store.get_by_user_code(user_code)
    if record is None or record.get("status") != "pending":
        return HTMLResponse(
            _render_activate_html(
                user_code=user_code,
                error="授权码无效或已过期，请在 CLI 重新发起。",
                principal=principal,
            ),
            status_code=400,
        )

    api_key_store: APIKeyStore = request.app.state.api_key_store
    key_expires = _device_flow_key_expires_at(config)
    key_result = await api_key_store.create(
        role=principal.role,
        scopes=sorted(principal.scopes),
        description=f"Device flow 为 {principal.user_id} 创建",
        expires_at=key_expires,
    )

    approved = await store.approve(
        user_code,
        principal={
            "user_id": principal.user_id,
            "display_name": principal.display_name,
            "email": principal.email,
            "role": principal.role,
            "scopes": sorted(principal.scopes),
        },
        api_key=key_result["key"],
    )
    if not approved:
        return HTMLResponse(
            _render_activate_html(
                user_code=user_code,
                error="授权处理失败，授权码可能已被他人使用。",
                principal=principal,
            ),
            status_code=400,
        )

    await _audit_auth(
        request,
        event_type="device_flow_approved",
        actor_id=principal.user_id,
        action="device_activate",
        outcome="success",
        details={"user_code": user_code, "key_id": key_result["key_id"]},
    )

    return HTMLResponse(
        _render_activate_html(
            user_code=user_code,
            success="授权成功，CLI 将在几秒内获得 API Key。",
            principal=principal,
        )
    )


@router.post("/device/token")
async def device_token(request: Request) -> dict[str, Any]:
    """CLI 轮询 token。"""
    _check_rate_limit(request)
    config: AppConfig = request.app.state.config
    if config.auth_mode != "oidc":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前认证模式不是 OIDC",
        )

    device_code = (await request.json()).get("device_code") if request.headers.get("content-type", "").startswith("application/json") else None
    if not device_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少 device_code",
        )

    store: DeviceFlowStore = request.app.state.device_flow_store
    record = await store.get_by_device_code(device_code)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="device_code 不存在",
        )

    # 如果已过期，直接告知完成（不再细分为 expired）
    expires_at = record.get("expires_at")
    try:
        expired = bool(expires_at and datetime.fromisoformat(expires_at) < datetime.now(timezone.utc))
    except ValueError:
        expired = True

    if expired or record.get("status") == "revoked":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="device_code 已过期或已吊销",
        )

    if record.get("status") != "approved":
        # 标准 OAuth2 device flow：返回 authorization_pending
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="authorization_pending",
        )

    api_key = record.get("api_key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="授权记录缺少 API Key",
        )

    await _audit_auth(
        request,
        event_type="device_flow_completed",
        actor_id=json.loads(record.get("principal") or "{}").get("user_id", "unknown"),
        action="device_token",
        outcome="success",
        details={"user_code": record.get("user_code")},
    )

    # 把 access_token 设为 API Key，CLI 拿到后可直接使用
    return {
        "access_token": api_key,
        "token_type": "Bearer",
        "expires_in": config.device_flow_api_key_expires_in_days * 86400,
    }


def _render_activate_html(
    *,
    user_code: str = "",
    error: str | None = None,
    success: str | None = None,
    principal: Principal | None = None,
) -> str:
    """渲染浏览器端 device flow 激活页。"""
    body_parts = [
        "<h1>授权 CLI 登录</h1>",
        "<p>请输入 CLI 上显示的用户授权码，然后点击批准。</p>",
    ]
    if error:
        body_parts.append(f'<div style="color:#dc2626;margin:12px 0;">{error}</div>')
    if success:
        body_parts.append(f'<div style="color:#15803d;margin:12px 0;">{success}</div>')
    if principal:
        body_parts.append(
            f"<p>当前登录用户：<strong>{principal.display_name or principal.user_id}</strong></p>"
        )

    body_parts.extend(
        [
            '<form method="post" action="/auth/device/activate">',
            '  <label for="user_code">用户授权码</label><br/>',
            '  <input id="user_code" name="user_code" type="text" '
            f'value="{user_code}" style="text-transform:uppercase;" required/><br/><br/>',
            '  <button type="submit">批准</button>',
            "</form>",
        ]
    )

    return (
        "<!DOCTYPE html><html><head>"
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">'
        "<title>授权 CLI</title></head><body>"
        + "".join(body_parts)
        + "</body></html>"
    )
