"""本地会话 Cookie 的读写与清理。

职责边界：只处理 Cookie 的签名、序列化与属性设置，不判断权限、不涉及 OIDC。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import AppConfig
from interfaces.web.auth.constants import (
    EPHEMERAL_COOKIE_MAX_AGE,
    EPHEMERAL_COOKIE_PATH,
)

logger = logging.getLogger(__name__)


def _session_serializer(config: AppConfig) -> URLSafeTimedSerializer:
    """返回本地会话签名器。

    Raises:
        ValueError: 密钥缺失或长度不足 32 字节。
    """
    secret = config.auth_session_secret
    if not secret or len(secret) < 32:
        raise ValueError("auth_session_secret 至少需要 32 字节")
    return URLSafeTimedSerializer(secret)


def get_session(request: Request) -> dict[str, Any] | None:
    """从签名 Cookie 中读取本地会话数据。

    Args:
        request: 当前请求。

    Returns:
        会话字典；无 Cookie 或签名无效/过期时返回 ``None``。
    """
    config: AppConfig = request.app.state.config
    raw = request.cookies.get(config.auth_cookie_name)
    if not raw:
        return None
    try:
        return _session_serializer(config).loads(
            raw, max_age=config.auth_session_max_age_seconds
        )
    except (BadSignature, SignatureExpired):
        # 签名不匹配或已过期都视为「未登录」，属于预期路径，不记异常日志
        return None


def set_session_cookie(response: Response, data: dict[str, Any], config: AppConfig) -> None:
    """把会话数据签名后写入 Cookie。

    Args:
        response: 待写入的响应对象。
        data: 会话数据。
        config: 应用配置，提供 Cookie 名、有效期与安全属性。
    """
    signed = _session_serializer(config).dumps(data)
    # WHY Secure 由配置决定：本地 HTTP 开发时浏览器会拒绝 Secure Cookie，
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


def set_ephemeral_cookie(
    response: Response,
    name: str,
    value: str,
    config: AppConfig,
    max_age: int = EPHEMERAL_COOKIE_MAX_AGE,
) -> None:
    """设置一次性签名 Cookie（如 PKCE verifier / state / next）。"""
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=True,
        secure=config.auth_cookie_secure,
        samesite=config.auth_cookie_samesite,
        path=EPHEMERAL_COOKIE_PATH,
    )


def get_ephemeral_cookie(request: Request, name: str) -> str | None:
    """读取一次性 Cookie。"""
    return request.cookies.get(name)


def clear_ephemeral_cookie(response: Response, name: str, config: AppConfig) -> None:
    """清除一次性 OIDC Cookie。

    WHY 路径必须与写入时一致：浏览器的 Cookie 匹配同时看域名与路径，
    路径不一致时删除指令不会生效。
    """
    response.set_cookie(
        key=name,
        value="",
        max_age=0,
        expires=0,
        httponly=True,
        secure=config.auth_cookie_secure,
        samesite=config.auth_cookie_samesite,
        path=EPHEMERAL_COOKIE_PATH,
    )
