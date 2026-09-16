"""OIDC 协议交互：Discovery、JWKS、ID Token 校验与授权码换取令牌。

职责边界：只处理与 IdP 的协议往来，不涉及本地会话与路由。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx
import jwt
from fastapi import HTTPException, status

from config import AppConfig

logger = logging.getLogger(__name__)

_cache: dict[str, Any] = {}
"""Discovery 与 JWKS 的进程内缓存。

WHY 缓存：这两份文档变更频率极低（通常仅在密钥轮换时变化），而每次登录都要
用到。不缓存会让每个登录请求多出两次外部 HTTP 往返，也会给 IdP 带来无谓压力。
"""


def discovery_url(issuer: str) -> str:
    """从 issuer 构造 Discovery URL，不修改 issuer 本身。"""
    return urljoin(issuer.rstrip("/") + "/", ".well-known/openid-configuration")


async def get_oidc_discovery(issuer: str, http: httpx.AsyncClient) -> dict[str, Any]:
    """拉取并缓存 OIDC Discovery 配置。

    Args:
        issuer: IdP 的 issuer URL。
        http: 复用的异步 HTTP 客户端。

    Returns:
        Discovery 文档内容。

    Raises:
        HTTPException: 503，IdP 不可达或返回错误状态。
    """
    cache_key = f"discovery:{issuer}"
    if cache_key in _cache:
        return _cache[cache_key]
    url = discovery_url(issuer)
    try:
        resp = await http.get(url, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("OIDC Discovery 失败：%s", url)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IdP 不可用",
        ) from exc
    data = resp.json()
    _cache[cache_key] = data
    return data


async def get_jwks(jwks_uri: str, http: httpx.AsyncClient) -> dict[str, Any]:
    """拉取并缓存 JWKS。

    Raises:
        HTTPException: 503，公钥端点不可达。
    """
    cache_key = f"jwks:{jwks_uri}"
    if cache_key in _cache:
        return _cache[cache_key]
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
    _cache[cache_key] = data
    return data


def verify_id_token(
    id_token: str,
    *,
    issuer: str,
    client_id: str,
    jwks: dict[str, Any],
    nonce: str | None = None,
) -> dict[str, Any]:
    """用 JWKS 验证 id_token 签名并校验关键 claim。

    WHY 固定 ``algorithms=["RS256"]``：若从 token 头部读取算法，攻击者可把
    ``alg`` 改为 ``none`` 或把 RSA 公钥当作 HMAC 密钥，实现签名伪造。

    Args:
        id_token: IdP 返回的 ID Token。
        issuer: 期望的签发者，应取自 Discovery 返回的 ``issuer``。
        client_id: 本应用的 client_id，用于校验 ``aud``。
        jwks: IdP 公钥集合。
        nonce: 若提供则校验 ``nonce`` claim，防止 ID Token 重放。

    Returns:
        校验通过的 claim 字典。

    Raises:
        HTTPException: 401，签名密钥缺失、公钥无法解析、token 过期/无效或 nonce 不匹配。
    """
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
        logger.exception("IdP 公钥解析失败：kid=%s", kid)
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


async def exchange_code(
    code: str,
    verifier: str | None,
    *,
    config: AppConfig,
    http: httpx.AsyncClient,
) -> dict[str, Any]:
    """用授权码换取 token 三元组。

    Args:
        code: IdP 回调携带的授权码。
        verifier: PKCE code_verifier；为 ``None`` 时不携带（兼容未启用 PKCE 的 IdP）。
        config: 应用配置，提供 client 凭据与回调地址。
        http: 复用的异步 HTTP 客户端。

    Returns:
        token 端点返回的 JSON。

    Raises:
        HTTPException: 503 缺少 token 端点；401 换取失败。
    """
    discovery = await get_oidc_discovery(config.oidc_issuer, http)
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
