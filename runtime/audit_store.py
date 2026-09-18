"""审计日志存储。

职责边界：只负责把审计事件落到数据库，不决定「什么该记」。
WHY 与 thread_store 共用同一个 SQLite：审计与会话是同一类持久化需求，分库会
引入第二套连接生命周期；且单节点场景下共享数据库文件便于备份。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_MAX_LIMIT = 200

_MAX_FETCH_LIMIT = 5000
"""单次扫描超期记录的最大条数。"""


def expiry_cutoff(retention_days: int) -> str:
    """计算保留期截止时间（UTC ISO-8601 秒级字符串）。

    WHY 抽成模块级函数：归档与删除必须使用**同一个**截止时间。若两处各算
    一次，第二次算出的时间更晚，就会删掉「已扫描但没归档」的那一小段记录，
    造成静默丢失。

    Args:
        retention_days: 保留天数，必须 >= 1。

    Returns:
        早于该时间的记录即视为超期。

    Raises:
        ValueError: ``retention_days`` 非法。
    """
    if not isinstance(retention_days, int) or isinstance(retention_days, bool):
        raise ValueError("retention_days 必须是整数")
    if retention_days < 1:
        raise ValueError("retention_days 不能小于 1")

    return (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
        timespec="seconds"
    )

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT NOT NULL,
    actor_id      TEXT NOT NULL,
    target_id     TEXT,
    action        TEXT,
    outcome       TEXT NOT NULL,
    ip            TEXT,
    user_agent    TEXT,
    trace_id      TEXT,
    details       TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_actor_time
    ON audit_log (actor_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_log_event_time
    ON audit_log (event_type, created_at DESC);

-- 按链路查「一次请求都做了什么」：没有这个索引就得全表扫 created_at
CREATE INDEX IF NOT EXISTS idx_audit_log_trace
    ON audit_log (trace_id, created_at DESC);
"""


class AuditStore:
    """审计日志的读写门面。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        if conn is None:
            raise ValueError("conn 不能为 None")

        self._conn = conn
        self._lock = asyncio.Lock()

    async def log(
        self,
        *,
        event_type: str,
        actor_id: str,
        target_id: str | None = None,
        action: str | None = None,
        outcome: str,
        ip: str | None = None,
        user_agent: str | None = None,
        trace_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """记录一条审计事件。

        WHY 独立方法而非直接 INSERT：所有审计字段统一落库，避免调用方漏写
        ``created_at``；同时 ``details`` 会自动 JSON 序列化。

        WHY ``trace_id`` 由调用方传入而不是本方法自己去读上下文：``runtime`` 层
        不得依赖 ``application``（分层契约 3），而上下文载体在应用层。IP/UA 走的是
        同一条路径，这里保持一致，不为一列数据破一次分层。
        """
        if not event_type or not actor_id or not outcome:
            raise ValueError("event_type、actor_id、outcome 不能为空")

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        async with self._lock:
            try:
                await self._conn.execute(
                    """
                    INSERT INTO audit_log
                        (event_type, actor_id, target_id, action, outcome, ip, user_agent,
                         trace_id, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_type,
                        actor_id,
                        target_id,
                        action,
                        outcome,
                        ip,
                        user_agent,
                        trace_id,
                        json.dumps(details, ensure_ascii=False, default=str) if details else None,
                        now,
                    ),
                )
                await self._conn.commit()
            except Exception:
                logger.exception("审计日志写入失败：%s actor=%s", event_type, actor_id)
                raise

    async def fetch_expired(
        self,
        *,
        cutoff: str,
        limit: int = 500,
        after_id: int = 0,
    ) -> list[dict[str, Any]]:
        """按主键游标分批读取超期事件，供归档使用。

        WHY 用 ``id`` 游标而不是 OFFSET：分批读取期间若插入新记录，OFFSET
        会整体错位导致漏读；而主键单调递增，游标只前进不回退，不会漏也不会
        重复。

        Args:
            cutoff: 截止时间（``expiry_cutoff`` 的返回值），早于它的记录被读取。
            limit: 单批条数，1..``_MAX_FETCH_LIMIT``。
            after_id: 只读取主键大于该值的记录。

        Returns:
            事件字典列表（含 ``id`` 字段），按主键升序；无更多记录时为空列表。

        Raises:
            ValueError: 参数非法。
            RuntimeError: 查询失败。
        """
        if not isinstance(cutoff, str) or not cutoff.strip():
            raise ValueError("cutoff 必须是非空字符串")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit 必须是整数")
        if not 1 <= limit <= _MAX_FETCH_LIMIT:
            raise ValueError(f"limit 必须在 1..{_MAX_FETCH_LIMIT} 之间")
        if not isinstance(after_id, int) or isinstance(after_id, bool) or after_id < 0:
            raise ValueError("after_id 必须是不小于 0 的整数")

        sql = """
            SELECT id, event_type, actor_id, target_id, action, outcome, ip, user_agent,
                   trace_id, details, created_at
            FROM audit_log
            WHERE created_at < ? AND id > ?
            ORDER BY id ASC
            LIMIT ?
        """
        async with self._lock:
            try:
                async with self._conn.execute(sql, (cutoff, after_id, limit)) as cursor:
                    rows = await cursor.fetchall()
            except Exception:
                logger.exception("读取超期审计记录失败：cutoff=%s after_id=%s", cutoff, after_id)
                raise

        return [dict(row) for row in rows]

    async def purge_expired(self, *, retention_days: int, cutoff: str | None = None) -> int:
        """删除超过保留期的审计记录。

        WHY 按 ``created_at`` 的字符串比较而不是日期函数：``created_at`` 以
        UTC ISO-8601 秒级字符串落库，同一格式的字符串比较与时序比较等价
        （``ORDER BY created_at`` 已在列表中依赖这一性质），无需让 SQLite
        做日期解析，也就不会受列类型与本地时区影响。

        Args:
            retention_days: 保留天数，必须 >= 1。
            cutoff: 已算好的截止时间；传入时直接使用，保证「归档时扫描的
                范围」与「删除的范围」完全一致。``None`` 时按保留期现算。

        Returns:
            实际删除的记录条数。

        Raises:
            ValueError: ``retention_days`` 小于 1，或 ``cutoff`` 非法。
            RuntimeError: 删除失败（连接异常、表被锁等）。
        """
        if not isinstance(retention_days, int) or isinstance(retention_days, bool):
            raise ValueError("retention_days 必须是整数")
        if retention_days < 1:
            raise ValueError("retention_days 不能小于 1")
        if cutoff is None:
            cutoff = expiry_cutoff(retention_days)
        elif not isinstance(cutoff, str) or not cutoff.strip():
            raise ValueError("cutoff 必须是非空字符串")

        async with self._lock:
            try:
                cursor = await self._conn.execute(
                    "DELETE FROM audit_log WHERE created_at < ?", (cutoff,)
                )
                await self._conn.commit()
            except Exception:
                logger.exception("审计日志清理失败：retention_days=%s cutoff=%s", retention_days, cutoff)
                raise

        deleted = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        if deleted:
            logger.info("审计日志清理完成：删除 %d 条（早于 %s）", deleted, cutoff)
        else:
            logger.debug("审计日志清理完成：无超期记录（早于 %s）", cutoff)
        return deleted

    async def count_all(self) -> int:
        """返回审计记录总数，供保留策略与后续指标使用。

        Returns:
            记录条数；表不可用时抛出 ``RuntimeError``。
        """
        async with self._lock:
            try:
                async with self._conn.execute("SELECT COUNT(*) AS total FROM audit_log") as cursor:
                    row = await cursor.fetchone()
            except Exception:
                logger.exception("统计审计日志条数失败")
                raise
        return int(row["total"]) if row is not None else 0

    async def list(
        self,
        *,
        actor_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按时间倒序列出审计事件。"""
        if limit < 1 or limit > _MAX_LIMIT:
            raise ValueError(f"limit 必须在 1..{_MAX_LIMIT} 之间")
        if offset < 0:
            raise ValueError("offset 不能为负数")

        conditions: list[str] = []
        params: list[Any] = []
        if actor_id:
            conditions.append("actor_id = ?")
            params.append(actor_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"""
            SELECT id, event_type, actor_id, target_id, action, outcome, ip, user_agent,
                   trace_id, details, created_at
            FROM audit_log
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        async with self._lock:
            async with self._conn.execute(sql, tuple(params)) as cursor:
                rows = await cursor.fetchall()

        return [dict(row) for row in rows]


@asynccontextmanager
async def open_audit_store(db_path: Path) -> AsyncIterator[AuditStore]:
    """以异步上下文的方式提供审计日志存储。"""
    if db_path is None:
        raise ValueError("db_path 不能为 None")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn: aiosqlite.Connection | None = None
    try:
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.executescript(_SCHEMA)
        try:
            # WHY 需要这条迁移：``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表补列，
            # 而升级前的库里已经有审计数据。重复执行必然抛「列已存在」，忽略即可。
            await conn.execute("ALTER TABLE audit_log ADD COLUMN trace_id TEXT;")
        except Exception:
            logger.debug("audit_log.trace_id 已存在，跳过迁移")
        await conn.commit()
        logger.info("审计日志表已就绪：%s", db_path)
        yield AuditStore(conn)
    except Exception:
        logger.exception("审计日志表初始化失败：%s", db_path)
        raise
    finally:
        if conn is not None:
            await conn.close()
