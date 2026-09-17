"""审计归档 sink：把即将被清理的审计事件导出为 JSONL 文件。

职责边界：只负责「把一批事件写成一个文件」，不决定删哪些（``AuditStore``）、
也不决定多久清一次（``AuditRetentionWorker``）。

WHY 用 JSONL 而不是 JSON 数组：归档可能横跨多个批次，数组格式必须等最后
一批到达才能闭合，一旦中途失败整个文件都不可用；JSONL 每行自洽，写入即
可读，追加与后续按行流式处理都天然成立。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

logger = logging.getLogger(__name__)

_ARCHIVE_SUFFIX = ".jsonl"
"""归档文件后缀。"""

_PART_SUFFIX = ".part"
"""写入中的临时后缀。

WHY 先写 ``.part`` 再改名：清理任务随后就会删除库里的记录，若归档文件在
写一半时就被外部采集程序读到，那份归档是残缺的；改名是原子操作，采集方
只会看到完整文件。
"""


class AuditArchiveBatch:
    """一次归档批次的文件写入器。

    生命周期由 :meth:`AuditArchive.batch` 管理：写入期间文件以 ``.part``
    存在，提交后改名为最终文件；未提交则临时文件被删除，不留半成品。
    """

    def __init__(self, path: Path) -> None:
        if path is None:
            raise ValueError("path 不能为 None")

        self._path = path
        self._part_path = path.with_name(path.name + _PART_SUFFIX)
        self._file: TextIO | None = None
        self._count = 0

    @property
    def path(self) -> Path:
        """最终归档文件路径（提交后才存在）。"""
        return self._path

    @property
    def count(self) -> int:
        """已写入的事件条数。"""
        return self._count

    async def write(self, events: Sequence[dict[str, Any]]) -> int:
        """追加写入一批事件。

        Args:
            events: 审计事件字典序列；空序列是 no-op。

        Returns:
            本次写入的条数。

        Raises:
            ValueError: ``events`` 为 ``None``。
            OSError: 目录不可写、磁盘已满等（已记日志后向上抛出）。
        """
        if events is None:
            raise ValueError("events 不能为 None")
        if not events:
            return 0

        # WHY 首次写入才打开文件：批次为空时不应在归档目录里留下 0 字节文件。
        if self._file is None:
            self._part_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = await asyncio.to_thread(
                self._part_path.open, "w", encoding="utf-8"
            )

        payload = "".join(
            json.dumps(event, ensure_ascii=False, default=str) + "\n" for event in events
        )
        try:
            # WHY 同步文件 I/O 走 to_thread：后台清理协程与 Web 请求共用事件
            # 循环，直接写大文件会把整个服务卡住。
            await asyncio.to_thread(self._write_and_sync, payload)
        except OSError:
            logger.exception("审计归档写入失败：%s", self._part_path)
            raise

        self._count += len(events)
        return len(events)

    def _write_and_sync(self, payload: str) -> None:
        """写入并刷盘；在线程中执行。"""
        if self._file is None:
            raise RuntimeError("归档文件尚未打开")
        self._file.write(payload)
        self._file.flush()
        # WHY 必须 fsync：归档一旦「写完」，随后就会删除库里的原始记录；
        # 仅 flush 只保证进了操作系统缓存，断电后归档与数据库两处皆空。
        os.fsync(self._file.fileno())

    async def commit(self) -> Path | None:
        """提交批次：关闭文件并去掉 ``.part`` 后缀。

        Returns:
            最终归档路径；本批次未写入任何事件时返回 ``None``。
        """
        await self._close_file()
        if self._count == 0:
            return None

        await asyncio.to_thread(self._part_path.replace, self._path)
        logger.info("审计归档完成：%s（%d 条）", self._path, self._count)
        return self._path

    async def discard(self) -> None:
        """丢弃批次：关闭并删除临时文件，保证不留半成品。"""
        await self._close_file()
        try:
            await asyncio.to_thread(self._part_path.unlink)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("审计归档临时文件删除失败：%s", self._part_path)

    async def _close_file(self) -> None:
        """关闭文件句柄；重复调用安全。"""
        file, self._file = self._file, None
        if file is not None:
            await asyncio.to_thread(file.close)


class AuditArchive:
    """审计归档目录的管理者。"""

    def __init__(self, directory: Path, *, prefix: str = "audit") -> None:
        """构造归档器。

        Args:
            directory: 归档目录；不存在时会在首次写入前创建。
            prefix: 归档文件名前缀。

        Raises:
            ValueError: ``directory`` 为 ``None`` 或 ``prefix`` 为空白。
        """
        if directory is None:
            raise ValueError("directory 不能为 None")
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValueError("prefix 必须是非空字符串")

        self._directory = Path(directory)
        self._prefix = prefix.strip()

    @property
    def directory(self) -> Path:
        """归档目录。"""
        return self._directory

    def build_path(self, now: datetime | None = None) -> Path:
        """构造归档文件路径。

        WHY 文件名带 UTC 时间戳与序号：同一秒内可能跑两次清理（手动触发 +
        定时任务），序号保证不互相覆盖；时间戳让运维按时间定位归档。
        """
        moment = now or datetime.now(timezone.utc)
        stamp = moment.strftime("%Y%m%dT%H%M%SZ")
        candidate = self._directory / f"{self._prefix}-{stamp}{_ARCHIVE_SUFFIX}"
        if not candidate.exists():
            return candidate

        sequence = 1
        while True:
            numbered = (
                self._directory / f"{self._prefix}-{stamp}-{sequence}{_ARCHIVE_SUFFIX}"
            )
            if not numbered.exists():
                return numbered
            sequence += 1

    @asynccontextmanager
    async def batch(self) -> AsyncIterator[AuditArchiveBatch]:
        """开启一个归档批次。

        正常退出时提交；抛出异常时丢弃临时文件并把异常继续向上抛，
        由调用方据此决定「不删除数据库记录」。

        Raises:
            OSError: 目录创建或文件写入失败。
        """
        batch = AuditArchiveBatch(self.build_path())
        try:
            yield batch
            await batch.commit()
        except Exception:
            await batch.discard()
            logger.exception("审计归档批次失败，已丢弃：%s", batch.path)
            raise


__all__ = ["AuditArchive", "AuditArchiveBatch"]
