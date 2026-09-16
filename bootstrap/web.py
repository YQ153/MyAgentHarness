"""Web 形态专有资源组装。

WHY 单独成模块：这些资源只有 Web 形态需要，放在 ``build_app_context`` 中会
让 CLI 承担无谓的构造开销，也会让「跨形态共享」与「Web 专有」两种依赖混淆。
"""

from __future__ import annotations

import httpx

from config import AppConfig
from runtime.rate_limiter import RateLimiter


def build_http_client() -> httpx.AsyncClient:
    """构造 OIDC 流程使用的 HTTP 客户端。

    WHY ``follow_redirects=False``：OIDC 的 Discovery 与 token 端点不应发生
    重定向。跟随重定向会把携带 ``client_secret`` 的请求转发到非预期地址，
    属于凭据外泄路径。
    """
    return httpx.AsyncClient(timeout=15.0, follow_redirects=False)


def build_rate_limiter(config: AppConfig) -> RateLimiter:
    """按配置构造认证端点限流器。

    Args:
        config: 应用配置，提供窗口长度与窗口内最大请求数。
    """
    if config is None:
        raise ValueError("config 不能为 None")

    return RateLimiter(
        window_seconds=config.auth_rate_limit_window_seconds,
        max_attempts=config.auth_rate_limit_max_attempts,
    )
