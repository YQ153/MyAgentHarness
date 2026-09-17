"""Web 形态专有资源组装。

WHY 单独成模块：这些资源只有 Web 形态需要，放在 ``build_app_context`` 中会
让 CLI 承担无谓的构造开销，也会让「跨形态共享」与「Web 专有」两种依赖混淆。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from config import AppConfig
from runtime.audit_archive import AuditArchive
from runtime.audit_retention import AuditRetentionWorker
from runtime.audit_store import AuditStore
from runtime.interval_worker import IntervalWorker
from runtime.rate_limiter import RateLimiter

if TYPE_CHECKING:
    from application.run_service import RunService


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

    WHY 归档器在这里构造：``audit_archive_enabled`` 关掉时传 ``None``，
    清理任务退化为「只删不导出」，无需在 runtime 层再判断开关。

    Raises:
        ValueError: ``config`` 或 ``audit_store`` 为 ``None``。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    if audit_store is None:
        raise ValueError("audit_store 不能为 None")

    archive = (
        AuditArchive(config.audit_archive_dir)
        if config.audit_archive_enabled
        else None
    )

    return AuditRetentionWorker(
        audit_store,
        retention_days=config.audit_retention_days,
        interval_seconds=config.audit_retention_interval_seconds,
        archive=archive,
        batch_size=config.audit_archive_batch_size,
    )


def build_run_governance_worker(config: AppConfig, run_service: RunService) -> IntervalWorker:
    """按配置构造运行治理的定期巡检任务。

    巡检内容（超时取消、审批挂起过期）由 ``RunService.enforce_governance``
    决定；这里只负责「多久做一次」与「怎么让它在事件循环里跑起来」。

    WHY 与审计清理同在此装配：两者都是只有长驻进程需要的后台协程，放在
    ``bootstrap.core`` 会让 CLI 这种一次性进程背上常驻协程与退出等待。

    Args:
        config: 应用配置，提供巡检间隔。
        run_service: 运行服务，提供 ``enforce_governance``。

    Returns:
        尚未启动的巡检任务；由调用方（Web 生命周期）``start()``。

    Raises:
        ValueError: ``config`` 或 ``run_service`` 为 ``None``。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    if run_service is None:
        raise ValueError("run_service 不能为 None")

    return IntervalWorker(
        run_service.enforce_governance,
        interval_seconds=config.run_governance_interval_seconds,
        name="run-governance",
        detail=f"，运行上限 {config.run_max_seconds} 秒，审批 TTL {config.hitl_pending_ttl_seconds} 秒",
    )
