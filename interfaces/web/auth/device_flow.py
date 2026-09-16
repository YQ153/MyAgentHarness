"""OIDC Device Authorization Grant，供无浏览器环境的 CLI 登录。

职责边界：管理设备授权码的签发、浏览器端批准与 CLI 轮询。批准后签发的
API Key 由 ``application.ports.APIKeyRepository`` 统一落库，本模块不直接操作数据库。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from application.ports import APIKeyRepository, DeviceFlowRepository
from application.principal import Principal
from config import AppConfig
from interfaces.web.auth.audit import log_auth_event
from interfaces.web.auth.deps import enforce_rate_limit, get_principal
from interfaces.web.auth.views import render_activate_html

logger = logging.getLogger(__name__)

router = APIRouter()


def _device_flow_expires_at(config: AppConfig) -> str:
    """按配置计算 device flow 过期时间（ISO8601 UTC）。"""
    delta = timedelta(seconds=config.device_flow_expires_in_seconds)
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


def _device_flow_key_expires_at(config: AppConfig) -> str:
    """按配置计算 device flow 派生 API Key 的过期时间（ISO8601 UTC）。"""
    delta = timedelta(days=config.device_flow_api_key_expires_in_days)
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


def _require_oidc_mode(config: AppConfig) -> None:
    """确认当前为 OIDC 模式。

    Raises:
        HTTPException: 400 当前认证模式不是 OIDC。
    """
    if config.auth_mode != "oidc":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前认证模式不是 OIDC",
        )


@router.post("/device/authorize")
async def device_authorize(request: Request) -> dict[str, Any]:
    """CLI 发起 device flow，返回 user_code 与轮询地址。

    Raises:
        HTTPException: 400 非 OIDC 模式；429 触发限流。
    """
    enforce_rate_limit(request)
    config: AppConfig = request.app.state.config
    _require_oidc_mode(config)

    store: DeviceFlowRepository = request.app.state.device_flow_store
    result = await store.create(expires_in_seconds=config.device_flow_expires_in_seconds)

    base_url = str(request.base_url).rstrip("/")
    verification_uri = f"{base_url}/auth/device/activate"

    await log_auth_event(
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
    enforce_rate_limit(request)
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
    store: DeviceFlowRepository = request.app.state.device_flow_store
    record = await store.get_by_user_code(user_code)
    if record is None or record.get("status") != "pending":
        return HTMLResponse(
            render_activate_html(
                user_code=user_code,
                error="授权码无效或已过期，请在 CLI 重新发起。",
            ),
            status_code=400,
        )

    return HTMLResponse(
        render_activate_html(
            user_code=user_code,
            principal=principal,
        )
    )


@router.post("/device/activate")
async def device_activate(
    request: Request,
    user_code: str = Form(...),
) -> HTMLResponse:
    """处理浏览器端的批准请求，并为 CLI 派生一枚 API Key。"""
    enforce_rate_limit(request)
    config: AppConfig = request.app.state.config
    if config.auth_mode != "oidc":
        return HTMLResponse("<h1>当前认证模式不是 OIDC</h1>", status_code=400)

    principal = await get_principal(request)
    if principal is None:
        return HTMLResponse(
            render_activate_html(
                user_code=user_code,
                error="登录会话已过期，请刷新页面后重试。",
            ),
            status_code=401,
        )

    store: DeviceFlowRepository = request.app.state.device_flow_store
    record = await store.get_by_user_code(user_code)
    if record is None or record.get("status") != "pending":
        return HTMLResponse(
            render_activate_html(
                user_code=user_code,
                error="授权码无效或已过期，请在 CLI 重新发起。",
                principal=principal,
            ),
            status_code=400,
        )

    api_key_store: APIKeyRepository = request.app.state.api_key_store
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
        # WHY 单独处理：approve 使用条件更新（status='pending'），失败说明
        # 同一 user_code 已被并发批准，属于需要显式告知用户的状态。
        return HTMLResponse(
            render_activate_html(
                user_code=user_code,
                error="授权处理失败，授权码可能已被他人使用。",
                principal=principal,
            ),
            status_code=400,
        )

    await log_auth_event(
        request,
        event_type="device_flow_approved",
        actor_id=principal.user_id,
        action="device_activate",
        outcome="success",
        details={"user_code": user_code, "key_id": key_result["key_id"]},
    )

    return HTMLResponse(
        render_activate_html(
            user_code=user_code,
            success="授权成功，CLI 将在几秒内获得 API Key。",
            principal=principal,
        )
    )


@router.post("/device/token")
async def device_token(request: Request) -> dict[str, Any]:
    """CLI 轮询 token。

    Raises:
        HTTPException: 400 缺少或非法的 device_code、仍待批准；500 记录缺少 API Key。
    """
    enforce_rate_limit(request)
    config: AppConfig = request.app.state.config
    _require_oidc_mode(config)

    content_type = request.headers.get("content-type", "")
    device_code = (
        (await request.json()).get("device_code")
        if content_type.startswith("application/json")
        else None
    )
    if not device_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少 device_code",
        )

    store: DeviceFlowRepository = request.app.state.device_flow_store
    record = await store.get_by_device_code(device_code)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="device_code 不存在",
        )

    # WHY 过期直接归为错误而非细分：RFC 8628 未定义 expired 错误码，
    # CLI 侧的降级路径与「无效码」一致，细分只会增加无收益的分支。
    expires_at = record.get("expires_at")
    try:
        expired = bool(
            expires_at and datetime.fromisoformat(expires_at) < datetime.now(timezone.utc)
        )
    except ValueError:
        logger.warning("device flow 过期时间格式非法：%s", expires_at)
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

    await log_auth_event(
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
