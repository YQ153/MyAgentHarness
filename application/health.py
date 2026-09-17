"""健康检查与运行指标。

职责边界：只回答「这个进程现在能不能干活」与「它干了多少活」，不参与任何
业务编排。探测结果必须由真实的依赖给出——凭配置猜出来的「健康」会让负载
均衡把流量打到一台连不上数据库的实例上。

WHY 独立于 ``RunService``：运行服务是热路径，为一次探活把它整个塞进依赖里
既无必要也放大故障面；而健康检查需要组合「数据库 + 模型 + 运行登记 + 审计」
四个数据源，本身就是一个跨聚合的读操作，独立成服务才能各自演进。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from application.dto import CheckResult, MetricsSnapshot, ReadinessReport

if TYPE_CHECKING:
    from application.model_catalog import ModelCatalog
    from application.run_service import RunService
    from config import AppConfig
    from runtime.audit_store import AuditStore
    from runtime.thread_store import ThreadMetaStore

logger = logging.getLogger(__name__)

DATABASE_CHECK = "database"
"""就绪探测中数据库检查项的标识。"""

MODEL_CHECK = "model"
"""就绪探测中默认模型检查项的标识。"""


class HealthService:
    """进程存活、就绪探测与运行指标。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        thread_store: ThreadMetaStore,
        run_service: RunService,
        audit_store: AuditStore | None = None,
        catalog: ModelCatalog | None = None,
        started_at: float | None = None,
    ) -> None:
        """构造健康检查服务。

        Args:
            config: 应用配置，提供默认模型别名。
            thread_store: 会话元数据存储，用于数据库连通性探测。
            run_service: 运行服务，提供运行中会话数、累计运行数与挂起审批数。
            audit_store: 审计存储，可选；为 ``None`` 时审计计数为 ``None``。
            catalog: 模型目录，可选；为 ``None`` 时跳过模型配置探测。
            started_at: 进程启动时刻（``time.monotonic`` 口径）；``None`` 表示
                以构造时刻为准。测试可显式传入以固定 uptime。

        Raises:
            ValueError: ``config`` / ``thread_store`` / ``run_service`` 为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if thread_store is None:
            raise ValueError("thread_store 不能为 None")
        if run_service is None:
            raise ValueError("run_service 不能为 None")

        self._config = config
        self._thread_store = thread_store
        self._run_service = run_service
        self._audit_store = audit_store
        self._catalog = catalog
        self._started_at = time.monotonic() if started_at is None else started_at

        logger.info("健康检查服务就绪：default_model=%s", config.default_model)

    @property
    def uptime_seconds(self) -> float:
        """进程已运行秒数。

        WHY 用 ``monotonic`` 而不是墙钟时间：系统时钟可能被 NTP 回拨，
        墙钟口径会出现负的运行时长。
        """
        return round(time.monotonic() - self._started_at, 3)

    async def readiness(self) -> ReadinessReport:
        """探测进程是否可以对外提供服务。

        探测项：数据库连通性（``SELECT 1``）与默认模型配置自洽性
        （别名已注册、密钥已提供、地址格式合法）。两者都不发起真实业务请求。

        Returns:
            各项检查结果与总判定；任一项不通过时 ``ready`` 为 ``False``。

        Raises:
            RuntimeError: 探测过程本身出现非预期异常（已记日志）。
        """
        logger.debug("开始就绪探测")
        try:
            checks = [
                await self._check_database(),
                self._check_model(),
            ]
        except Exception:
            # WHY 记日志后向上抛：探测逻辑本身出错不能冒充「已就绪」，
            # 让调用方返回 500 反而比静默放行更容易被发现。
            logger.exception("就绪探测执行失败")
            raise

        ready = all(check.ok for check in checks)
        if ready:
            logger.debug("就绪探测通过")
        else:
            logger.warning(
                "就绪探测未通过：%s",
                [f"{check.name}: {check.detail}" for check in checks if not check.ok],
            )
        return ReadinessReport(ready=ready, checks=checks)

    async def metrics(self) -> MetricsSnapshot:
        """采集运行指标快照。

        Returns:
            运行中会话数、累计运行数、等待审批数、审计事件总数与进程运行时长；
            审计存储不可用或查询失败时 ``audit_events`` 为 ``None``。
        """
        audit_events: int | None = None
        if self._audit_store is not None:
            try:
                audit_events = await self._audit_store.count_all()
            except Exception:
                # WHY 单独降级而不让整个端点失败：审计计数是旁路指标，
                # 它挂掉不应连带让「运行中会话数」这类核心指标也不可见；
                # 采集失败以 ``None`` 表达，区别于「确实为 0 条」。
                logger.exception("采集审计事件总数失败，本轮指标该项置空")

        snapshot = MetricsSnapshot(
            running_threads=len(self._run_service.running_thread_ids()),
            started_runs=self._run_service.started_runs,
            pending_hitl=len(self._run_service.pending_hitl_thread_ids()),
            audit_events=audit_events,
            uptime_seconds=self.uptime_seconds,
        )
        logger.debug("运行指标采集完成：%s", snapshot.model_dump())
        return snapshot

    # ------------------------------------------------------------------ 内部

    async def _check_database(self) -> CheckResult:
        """探测数据库连通性。"""
        try:
            await self._thread_store.ping()
        except Exception as exc:
            logger.exception("数据库连通性探测失败")
            return CheckResult(
                name=DATABASE_CHECK,
                ok=False,
                detail=f"数据库不可访问：{exc}",
            )
        return CheckResult(name=DATABASE_CHECK, ok=True)

    def _check_model(self) -> CheckResult:
        """探测默认模型配置是否自洽。"""
        if self._catalog is None:
            # WHY 缺失目录不算不健康：CLI 等形态不装配模型目录也能正常跑，
            # 把它判成失败会让非 Web 形态永远处于未就绪状态。
            return CheckResult(name=MODEL_CHECK, ok=True, detail="未装配模型目录，已跳过")

        probe: Any = self._catalog.probe(self._config.default_model)
        if getattr(probe, "ok", False):
            return CheckResult(name=MODEL_CHECK, ok=True)

        detail = str(getattr(probe, "detail", "") or "默认模型配置不可用")
        logger.warning("默认模型配置探测未通过：%s", detail)
        return CheckResult(name=MODEL_CHECK, ok=False, detail=detail)


__all__ = ["DATABASE_CHECK", "MODEL_CHECK", "HealthService"]
