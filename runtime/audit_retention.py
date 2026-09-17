"""审计日志保留期的定期清理任务。

职责边界：只负责「按配置周期调用一次清理」，不决定保留多久（由
``AppConfig`` 决定），也不决定删哪些记录（由 ``AuditStore.purge_expired``
决定）。

WHY 放在 ``runtime`` 而不是 ``bootstrap``：它是一个可独立启停、可单独测试的
后台协程；``bootstrap`` 只负责按配置把它装配到宿主（当前是 Web 形态的
lifespan）上。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from runtime.audit_store import expiry_cutoff
from runtime.interval_worker import IntervalWorker

if TYPE_CHECKING:
    from pathlib import Path

    from runtime.audit_archive import AuditArchive
    from runtime.audit_store import AuditStore

logger = logging.getLogger(__name__)


class AuditRetentionWorker(IntervalWorker):
    """按固定间隔清理超过保留期的审计记录。

    幂等与可重入：``start`` 重复调用不会起第二个协程；``stop`` 在未启动或
    已停止时是 no-op，因此在 lifespan 的 ``finally`` 中可以无条件调用。

    Args:
        audit_store: 审计存储，提供 ``purge_expired`` 与 ``fetch_expired``。
        retention_days: 保留天数，必须 >= 1。
        interval_seconds: 清理间隔秒数，必须 >= 1。
        archive: 归档 sink；``None`` 表示不做归档、直接删除。
        batch_size: 归档时每批读取的条数，必须 >= 1。

    Raises:
        ValueError: 任一依赖为 ``None``，或参数越界。
    """

    def __init__(
        self,
        audit_store: AuditStore,
        *,
        retention_days: int,
        interval_seconds: int,
        archive: AuditArchive | None = None,
        batch_size: int = 500,
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
        if not isinstance(batch_size, int) or isinstance(batch_size, bool):
            raise ValueError("batch_size 必须是整数")
        if batch_size < 1:
            raise ValueError("batch_size 不能小于 1")

        # WHY 复用 IntervalWorker 的循环：启动即执行、失败不退出、停止时
        # cancel 后 await 这三件事由骨架统一保证，本类只保留「清理什么」。
        super().__init__(
            self.prune_once,
            interval_seconds=interval_seconds,
            name="audit-retention",
            detail=f"，保留 {retention_days} 天",
        )
        self._audit_store = audit_store
        self._retention_days = retention_days
        self._archive = archive
        self._batch_size = batch_size

    async def prune_once(self) -> int:
        """执行一次清理：先归档、再删除，返回删除条数。

        WHY 先归档后删除，且两者共用同一个 ``cutoff``：归档与删除是同一批
        记录的两次处理，若各自计算截止时间，后算的那个更晚，就会删掉
        「扫过但没归档」的夹缝记录——那时数据已经不在库里，归档也补不回来。

        WHY 归档失败时不删除（fail-closed）：归档目录不可写的情况下继续删，
        等于用「清理任务成功」的假象掩盖数据丢失；让异常向上冒，本轮清理
        跳过，超期记录留到下一轮，运维从 ERROR 日志里能看到并修目录权限。

        Returns:
            删除的审计记录条数；归档或删除失败时异常向上传播（由 ``_run`` 收敛）。
        """
        cutoff = expiry_cutoff(self._retention_days)
        if self._archive is None:
            deleted = await self._audit_store.purge_expired(
                retention_days=self._retention_days, cutoff=cutoff
            )
            if deleted:
                logger.info("审计日志保留清理：删除 %d 条超期记录", deleted)
            return deleted

        archived_path, archived = await self._archive_expired(cutoff)
        if archived == 0:
            logger.debug("审计日志清理：无超期记录（早于 %s）", cutoff)
            return 0

        deleted = await self._audit_store.purge_expired(
            retention_days=self._retention_days, cutoff=cutoff
        )
        logger.info(
            "审计日志清理完成：归档 %d 条到 %s，删除 %d 条（早于 %s）",
            archived,
            archived_path,
            deleted,
            cutoff,
        )
        if deleted != archived:
            # 归档与删除之间可能有并发写入（新记录不会落入 cutoff 之前，
            # 但另一个实例的清理可能抢先删过），数量不一致时必须留痕。
            logger.warning(
                "审计归档与删除条数不一致：归档 %d 条、删除 %d 条（早于 %s）",
                archived,
                deleted,
                cutoff,
            )
        return deleted

    async def _archive_expired(self, cutoff: str) -> tuple[Path | None, int]:
        """把截止时间之前的事件分批写入归档文件。

        Args:
            cutoff: 截止时间。

        Returns:
            ``(归档文件路径, 条数)``；无超期记录时路径为 ``None``、条数为 0。

        Raises:
            OSError: 归档目录不可写等；由调用方据此跳过删除。
        """
        async with self._archive.batch() as batch:
            after_id = 0
            while True:
                events = await self._audit_store.fetch_expired(
                    cutoff=cutoff, limit=self._batch_size, after_id=after_id
                )
                if not events:
                    break

                await batch.write(events)
                after_id = int(events[-1]["id"])
                if len(events) < self._batch_size:
                    break

        return (batch.path if batch.count else None), batch.count


__all__ = ["AuditRetentionWorker"]
