"""运行治理：按时间推进的强制收口动作（运行超时取消、审批挂起超期作废）。

职责边界：只做「巡检一次并做出决定」，不推进运行、不翻译事件。它读运行登记表、
写审计、终止卡住的命令进程树，除此之外不碰任何状态。

WHY 与 ``RunService`` 分开：这两件事都不是用户触发的，而是后台协程按时间推进
自动发生的，它们的正确性取决于「判定时机是否准确」而不是「调用是否正确」——
这一点从测试也被单独分成 ``test_run_governance.py`` 就能看出。放在一起时，
它们的存在只会让运行主流程的阅读者多跨过一百多行与「发起对话」无关的逻辑。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from application.audit_recorder import AuditRecorder
from application.dto import GovernanceReport
from application.run_registry import STOP_REASON_TIMEOUT, RunHandle, RunRegistry
from runtime.execution_registry import abort_scope

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

SYSTEM_ACTOR = "system"
"""后台治理动作的审计主体标识。

WHY 不用空串：审计表的 ``actor_id`` 为空表示「未知」，而后台治理确实是
系统做出的决定，二者在事后追溯时含义完全不同。
"""


class RunGovernor:
    """按阈值收口失控的运行与审批挂起。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        registry: RunRegistry,
        audit: AuditRecorder,
    ) -> None:
        """构造治理器。

        Args:
            config: 应用配置，提供 ``run_max_seconds`` 与
                ``hitl_pending_ttl_seconds`` 两个阈值。
            registry: 运行登记表；巡检快照与计数自增都经它进行。
            audit: 审计写入通道，取 ``RunService._audit``。

        Raises:
            ValueError: 任一必需依赖为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if registry is None:
            raise ValueError("registry 不能为 None")
        if audit is None:
            raise ValueError("audit 不能为 None")

        self._config = config
        self._registry = registry
        self._audit = audit

    async def enforce(self) -> GovernanceReport:
        """巡检一次运行治理：强制取消超时运行、作废超期未决策的审批挂起。

        由后台协程按 ``run_governance_interval_seconds`` 调用；也允许运维在
        测试中手动触发一次。

        WHY 把两件事放在一次巡检里：它们共享同一份「运行即时状态」的快照，
        也共享同一条审计口径（动作主体都是系统）；分成两个协程就要两把锁的
        快照语义，反而更容易出现「刚判定超时、同一轮又判它挂起过期」的
        自相矛盾记录。

        Returns:
            本次巡检的结果（超时数、过期数、巡检到的运行/挂起数）。

        Raises:
            RuntimeError: 阈值配置非法（由配置校验兜底，理论不可达）。
        """
        limit = self._config.run_max_seconds
        ttl = self._config.hitl_pending_ttl_seconds
        if not isinstance(limit, int) or limit < 0:
            raise RuntimeError("run_max_seconds 配置非法")
        if not isinstance(ttl, int) or ttl < 0:
            raise RuntimeError("hitl_pending_ttl_seconds 配置非法")

        now = time.monotonic()
        running_snapshot = self._registry.handles()
        pending_snapshot = self._registry.pending_hitl_ids()
        report = GovernanceReport(
            checked_runs=len(running_snapshot),
            checked_hitl=len(pending_snapshot),
        )

        report.timed_out_runs = await self.enforce_timeouts(running_snapshot, limit, now)
        report.expired_hitl = await self.expire_stale_hitl(pending_snapshot, ttl)

        if report.timed_out_runs or report.expired_hitl:
            logger.info(
                "运行治理巡检：超时取消 %d 个运行，作废 %d 个超期审批",
                report.timed_out_runs,
                report.expired_hitl,
            )
        return report

    async def enforce_timeouts(
        self,
        running_snapshot: dict[str, RunHandle],
        limit: int,
        now: float,
    ) -> int:
        """强制取消超过 ``run_max_seconds`` 的运行，返回被取消的数量。

        Args:
            running_snapshot: 本轮巡检看到的运行句柄快照。
            limit: 运行时长上限（秒）；``<= 0`` 表示显式关闭超时。
            now: 快照对应的 ``time.monotonic()`` 时刻。
        """
        if limit <= 0:
            return 0

        cancelled = 0
        for thread_id, handle in running_snapshot.items():
            if handle.stop_requested or (now - handle.started_at) < limit:
                continue
            # WHY 二次确认句柄仍在册：快照到此刻之间该运行可能已自然结束，
            # 若直接置位，就会对一个已废弃的句柄记一次超时审计。
            if self._registry.handle(thread_id) is not handle:
                continue

            elapsed = handle.elapsed_seconds
            handle.request_stop(STOP_REASON_TIMEOUT)
            # WHY 超时同样要终止子进程树：超时往往正是命令卡住造成的，
            # 只取消 future 会让那棵进程树继续活到它自己的超时。
            aborted = abort_scope(thread_id)
            self._registry.record_timeout()
            cancelled += 1
            logger.warning(
                "会话 %s 运行超过 %d 秒（实际 %.1f 秒），已强制取消，终止在跑命令 %d 个",
                thread_id,
                limit,
                elapsed,
                aborted,
            )
            await self._audit(
                event_type="run_timeout",
                actor_id=SYSTEM_ACTOR,
                target_id=thread_id,
                action="timeout",
                outcome="success",
                details={
                    "elapsed_seconds": round(elapsed, 3),
                    "max_seconds": limit,
                },
            )
        return cancelled

    async def expire_stale_hitl(
        self,
        pending_snapshot: tuple[str, ...],
        ttl: int,
    ) -> int:
        """作废挂起超过 ``hitl_pending_ttl_seconds`` 的审批，返回作废数量。

        WHY 用「当前挂起时长」而不是传入的统一 ``now`` 再减：判定与作废之间
        隔着一次 await（写审计），期间用户完全可能应答；以登记表内的实时时长
        为准，可以让刚刚被应答的会话不会被误判。
        """
        if ttl <= 0:
            return 0

        expired = 0
        for thread_id in pending_snapshot:
            age = self._registry.hitl_pending_age(thread_id)
            if age is None or age < ttl:
                continue
            # WHY 以 expire_hitl_pending 的返回值为准：用户可能刚好在这一刻
            # 应答（resume 会先清登记），此时本次巡检应当让位，而不是把一次
            # 已经生效的审批再标记成过期。
            if not self._registry.expire_hitl_pending(thread_id):
                continue
            expired += 1
            await self._audit(
                event_type="hitl_expired",
                actor_id=SYSTEM_ACTOR,
                target_id=thread_id,
                action="expire",
                outcome="success",
                details={
                    "pending_seconds": round(age, 3),
                    "ttl_seconds": ttl,
                },
            )
        return expired
