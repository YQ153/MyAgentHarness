"""Web 形态专有资源组装。

WHY 单独成模块：这些资源只有 Web 形态需要，放在 ``build_app_context`` 中会
让 CLI 承担无谓的构造开销，也会让「跨形态共享」与「Web 专有」两种依赖混淆。
"""

from __future__ import annotations

import httpx

from config import AppConfig
from runtime.audit_retention import AuditRetentionWorker
from runtime.audit_store import AuditStore
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


def build_audit_retention_worker(config: AppConfig, audit_store: AuditStore) -> AuditRetentionWorker:
    """按配置构造审计保留期的定期清理任务。

    WHY 在此装配而不是在 ``bootstrap.core``：只有长驻进程（Web）需要周期
    清理；放进共享装配会让 CLI 这种一次性进程也背上常驻协程与退出等待。

    Args:
        config: 应用配置，提供保留天数与清理间隔。
        audit_store: 审计存储。

    Returns:
        尚未启动的清理任务；由调用方（Web lifespan）``start()``。

    Raises:
        ValueError: ``config`` 或 ``audit_store`` 为 ``None``。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    if audit_store is None:
        raise ValueError("audit_store 不能为 None")

    return AuditRetentionWorker(
        audit_store,
        retention_days=config.audit_retention_days,
        interval_seconds=config.audit_retention_interval_seconds,
    )
