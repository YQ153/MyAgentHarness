"""审计日志保留期的定期清理任务。

职责边界：只负责「按配置周期调用一次清理」，不决定保留多久（由
``AppConfig`` 决定），也不决定删哪些记录（由 ``AuditStore.purge_expired``
决定）。

WHY 放在 ``runtime`` 而不是 ``bootstrap``：它是一个可独立启停、可单独测试的
后台协程；``bootstrap`` 只负责按配置把它装配到宿主（当前是 Web 形态的
lifespan）上。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.audit_store import AuditStore

logger = logging.getLogger(__name__)


class AuditRetentionWorker:
    """按固定间隔清理超过保留期的审计记录。

    幂等与可重入：``start`` 重复调用不会起第二个协程；``stop`` 在未启动或
    已停止时是 no-op，因此在 lifespan 的 ``finally`` 中可以无条件调用。

    Args:
        audit_store: 审计存储，提供 ``purge_expired``。
        retention_days: 保留天数，必须 >= 1。
        interval_seconds: 清理间隔秒数，必须 >= 1。

    Raises:
        ValueError: 任一依赖为 ``None``，或参数越界。
    """

    def __init__(
        self,
        audit_store: AuditStore,
        *,
        retention_days: int,
        interval_seconds: int,
    ) -> None:
        if audit_store is None:
            raise ValueError("audit_store 不能为 None")
        if not isinstance(retention_days, int) or isinstance(retention_days, bool):
            raise ValueError("retention_days 必须是整数")
        if retention_days < 1:
            raise ValueError("retention_days 不能小于 1")
        if not isinstance(interval_seconds, int) or isinstance(interval_seconds, bool):
            raise ValueError("interval_seconds 必须是整数")
        if interval_seconds < 1:
            raise ValueError("interval_seconds 不能小于 1")

        self._audit_store = audit_store
        self._retention_days = retention_days
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """清理协程是否在运行。"""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """启动清理协程；已运行时不做任何事。

        WHY 先立即执行一次清理：服务重启往往发生在长时间停机之后，
        堆着超期记录等到下一个间隔才删，会让首个间隔内表仍然臃肿。

        Raises:
            RuntimeError: 当前没有运行中的事件循环（在同步上下文里调用）。
        """
        if self.is_running:
            logger.debug("审计保留清理任务已在运行，忽略重复启动")
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("AuditRetentionWorker.start() 必须在运行中的事件循环内调用") from exc

        self._task = loop.create_task(self._run(), name="audit-retention")
        logger.info(
            "审计保留清理任务已启动：保留 %d 天，间隔 %d 秒",
            self._retention_days,
            self._interval_seconds,
        )

    async def stop(self) -> None:
        """停止清理协程并等待其退出；未启动时直接返回。

        WHY 取消后必须 await：只在 ``finally`` 里 ``cancel()`` 而不等待，
        协程可能仍在执行 DELETE，进程关闭阶段会撞上已关闭的连接，并留下
        "Task was destroyed but it is pending" 告警。
        """
        task = self._task
        self._task = None
        if task is None:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.info("审计保留清理任务已停止")
        except Exception:
            # WHY 记录后吞掉：停止阶段的失败无处可上报，若让它冒泡会掩盖
            # lifespan 中更关键的关闭异常。
            logger.exception("审计保留清理任务异常退出")
        finally:
            self._task = None

    async def prune_once(self) -> int:
        """执行一次清理，返回删除条数。

        Returns:
            删除的审计记录条数；存储层失败时异常向上传播（由 ``_run`` 收敛）。
        """
        deleted = await self._audit_store.purge_expired(retention_days=self._retention_days)
        if deleted:
            logger.info("审计日志保留清理：删除 %d 条超期记录", deleted)
        return deleted

    async def _run(self) -> None:
        """清理主循环：清理 → 等待 → 再清理，直到被取消。

        WHY 单次失败不退出循环：清理是旁路运维能力，一次数据库抖动就永久
        停掉它，会让「以为在自动清理」变成静默的磁盘增长；失败后按原间隔
        重试，并把异常留在日志里供告警。
        """
        try:
            while True:
                try:
                    await self.prune_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "审计日志保留清理失败，将在 %d 秒后重试", self._interval_seconds
                    )
                await asyncio.sleep(self._interval_seconds)
        except asyncio.CancelledError:
            logger.info("审计保留清理任务收到取消信号，正在退出")
            raise


__all__ = ["AuditRetentionWorker"]
