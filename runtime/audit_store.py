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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_MAX_LIMIT = 200

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
    details       TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_actor_time
    ON audit_log (actor_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_log_event_time
    ON audit_log (event_type, created_at DESC);
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
        details: dict[str, Any] | None = None,
    ) -> None:
        """记录一条审计事件。

        WHY 独立方法而非直接 INSERT：所有审计字段统一落库，避免调用方漏写
        ``created_at``；同时 ``details`` 会自动 JSON 序列化。
        """
        if not event_type or not actor_id or not outcome:
            raise ValueError("event_type、actor_id、outcome 不能为空")

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        async with self._lock:
            try:
                await self._conn.execute(
                    """
                    INSERT INTO audit_log
                        (event_type, actor_id, target_id, action, outcome, ip, user_agent, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_type,
                        actor_id,
                        target_id,
                        action,
                        outcome,
                        ip,
                        user_agent,
                        json.dumps(details, ensure_ascii=False, default=str) if details else None,
                        now,
                    ),
                )
                await self._conn.commit()
            except Exception:
                logger.exception("审计日志写入失败：%s actor=%s", event_type, actor_id)
                raise

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
            SELECT id, event_type, actor_id, target_id, action, outcome, ip, user_agent, details, created_at
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
        await conn.commit()
        logger.info("审计日志表已就绪：%s", db_path)
        yield AuditStore(conn)
    except Exception:
        logger.exception("审计日志表初始化失败：%s", db_path)
        raise
    finally:
        if conn is not None:
            await conn.close()
