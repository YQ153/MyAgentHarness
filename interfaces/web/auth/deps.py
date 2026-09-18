"""身份提取与权限校验依赖项。

职责边界：把 HTTP 请求转换为 ``Principal``，并充当路由层的权限闸门。
本模块是「认证」与「鉴权」在 Web 层的唯一入口——任何路由都必须经过
``require_permission``，不允许自写角色判断。
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from application.ports import APIKeyRepository, RateLimiterPort
from application.principal import (
    ANONYMOUS_PRINCIPAL,
    Principal,
    ROLE_PERMISSIONS,
)
from config import AppConfig
from interfaces.web.auth.audit import log_auth_event
from interfaces.web.auth.utils import bearer_token, client_ip

logger = logging.getLogger(__name__)


def enforce_rate_limit(request: Request) -> None:
    """认证端点限流检查。

    WHY 按 IP 而非按账号：暴力破解可以轮换用户名，按账号限流会漏掉这种情况；
    按 IP 限流能兜住自动化扫描，而正常用户极少在窗口内触发阈值。

    Raises:
        HTTPException: 429，窗口内请求数超过阈值。
    """
    limiter: RateLimiterPort | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    ip = client_ip(request)
    if not limiter.is_allowed(ip):
        logger.warning("认证端点触发限流：ip=%s", ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试",
        )


async def get_principal(request: Request) -> Principal | None:
    """提取当前请求的认证主体。

    - ``disabled`` 模式：返回匿名管理员主体（仅推荐本地开发）。
    - ``apikey`` 模式：从配置的请求头或 ``Authorization: Bearer`` 读取 API Key。

    Args:
        request: 当前请求。

    Returns:
        已认证的 ``Principal``；未认证时返回 ``None``。
    """
    config: AppConfig = request.app.state.config

    if config.auth_mode == "disabled":
        return ANONYMOUS_PRINCIPAL

    if config.auth_mode == "apikey":
        api_key = request.headers.get(config.auth_api_key_header) or bearer_token(request)
        if api_key:
            return await _validate_api_key(request, api_key)
        return None

    logger.warning("未知的 auth_mode：%s，按未认证处理", config.auth_mode)
    return None


async def _validate_api_key(request: Request, api_key: str) -> Principal | None:
    """校验 API Key 并返回对应主体。

    Returns:
        校验通过的主体；校验失败返回 ``None``。
    """
    store: APIKeyRepository | None = getattr(request.app.state, "api_key_store", None)
    config: AppConfig = request.app.state.config

    # WHY 兜底 dev key：最小可用与单节点场景下保留环境变量快速入口，
    # 生产环境应把 dev key 置空，强制走数据库 key store。
    if config.auth_api_key_dev and secrets.compare_digest(api_key, config.auth_api_key_dev):
        await log_auth_event(
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
        await log_auth_event(
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
        await log_auth_event(
            request,
            event_type="apikey_auth_failure",
            actor_id="unknown",
            action="validate",
            outcome="failure",
            details={"reason": "invalid_or_revoked"},
        )
        return None

    actor_id = f"apikey:{record['key_id']}"
    await log_auth_event(
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


def require_permission(permission: str):
    """路由层权限校验依赖项工厂。

    WHY 用依赖项而非中间件：权限要求因端点而异，中间件只能做粗粒度判断；
    依赖项可以贴着路由声明，评审时一眼可见该端点需要什么权限。

    Args:
        permission: 所需权限标识。

    Returns:
        FastAPI 依赖项；未认证抛 401，权限不足抛 403。
    """

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
            await log_auth_event(
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
