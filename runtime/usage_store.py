"""Token 用量存储。

职责边界：只负责「把一次运行的用量写进去、按维度聚合出来」，不决定什么
算一次运行（由 ``RunService`` 决定），也不决定谁能看哪些数据（由
``application.usage_service`` 决定）。

WHY 与会话元数据共用同一个 SQLite：用量记录天然要按会话与所有者聚合，
分库会让「会话已删除、用量还在」这类跨库不一致无法用一条 SQL 发现。
并发模型与 ``thread_store`` 一致：所有语句都在一把 ``asyncio.Lock`` 下执行。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

MAX_MODEL_CHARS = 128
_MAX_OWNER_CHARS = 128
_MAX_TOKEN_VALUE = 100_000_000
"""单个计数的硬上限。

WHY 需要一个上限：provider 侧异常时可能返回荒谬的数值（例如把字节数当
token 数），写进库后所有聚合都会失真；上限拦不住故障，但能让故障可见。
"""

_GROUP_COLUMNS: dict[str, str] = {
    "model": "model",
    "thread": "thread_id",
    "day": "substr(created_at, 1, 10)",
}
"""聚合维度白名单。

WHY 用白名单映射而不是把入参拼进 SQL：``group_by`` 直接来自查询串，
任何拼接写法都是注入面；白名单把可选值收敛为三条固定表达式。
"""


def utc_now() -> str:
    """返回可直接按字典序比较的 ISO8601 UTC 时间串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def window_start(days: int) -> str:
    """计算时间窗起点（UTC ISO-8601 秒级字符串）。

    Args:
        days: 向前回看的天数，必须 >= 1。

    Returns:
        早于该时间的记录不计入统计。

    Raises:
        ValueError: ``days`` 非法。
    """
    if not isinstance(days, int) or isinstance(days, bool):
        raise ValueError("days 必须是整数")
    if days < 1:
        raise ValueError("days 不能小于 1")

    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id         TEXT NOT NULL,
    owner_id          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    trace_id          TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_log_owner_time
    ON usage_log (owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_usage_log_thread_time
    ON usage_log (thread_id, created_at DESC);
"""

_TRACE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_usage_log_trace
    ON usage_log (trace_id, created_at DESC);
"""
"""trace_id 的索引。

WHY 单独放在这里而不是写进 ``_SCHEMA``：``CREATE TABLE IF NOT EXISTS`` 对老库不做
任何事，老库的 usage_log 里还没有 trace_id 这一列——索引若排在补列的 ALTER 之前，
``executescript`` 会以「no such column」失败，**应用直接起不来**。
"""


class UsageStore:
    """Token 用量的写入与聚合门面。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        """构造存储。

        Args:
            conn: 已建表的异步连接。

        Raises:
            ValueError: ``conn`` 为 ``None``。
        """
        if conn is None:
            raise ValueError("conn 不能为 None")

        self._conn = conn
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 写入

    async def record(
        self,
        *,
        thread_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        owner_id: str = "",
        trace_id: str | None = None,
        created_at: str | None = None,
    ) -> int:
        """写入一条用量记录。

        Args:
            thread_id: 会话 ID。
            model: 模型别名（不是 provider 侧的模型名，后者可由别名反查）。
            prompt_tokens: 输入 token 数。
            completion_tokens: 输出 token 数。
            owner_id: 会话所有者；认证关闭时为空串。
            trace_id: 本次请求的链路标识；``None`` 表示未知（例如 CLI 形态）。
            created_at: 落库时间；``None`` 表示取当前 UTC 时间。

        Returns:
            新记录的主键。

        Raises:
            ValueError: 任一参数非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized_thread = self._validate_thread_id(thread_id)
        normalized_model = self._validate_model(model)
        normalized_owner = self._validate_owner(owner_id)
        prompt = self._validate_tokens("prompt_tokens", prompt_tokens)
        completion = self._validate_tokens("completion_tokens", completion_tokens)
        timestamp = created_at if isinstance(created_at, str) and created_at.strip() else utc_now()

        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    INSERT INTO usage_log
                        (thread_id, owner_id, model, prompt_tokens, completion_tokens,
                         trace_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_thread,
                        normalized_owner,
                        normalized_model,
                        prompt,
                        completion,
                        trace_id,
                        timestamp,
                    ),
                ) as cursor:
                    row_id = int(cursor.lastrowid or 0)
                await self._conn.commit()
            except Exception:
                logger.exception("用量记录写入失败：thread=%s model=%s", normalized_thread, normalized_model)
                raise

        logger.debug(
            "用量已记录：thread=%s model=%s prompt=%d completion=%d",
            normalized_thread,
            normalized_model,
            prompt,
            completion,
        )
        return row_id

    # ------------------------------------------------------------------ 查询

    async def summarize(
        self,
        *,
        owner_id: str | None = None,
        thread_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        group_by: str = "model",
    ) -> dict[str, Any]:
        """按维度聚合用量。

        Args:
            owner_id: 只统计该所有者；``None`` 表示不限制。
            thread_id: 只统计该会话；``None`` 表示不限制。
            since: 时间窗起点（含），``None`` 表示不限起点。
            until: 时间窗终点（不含），``None`` 表示不限终点。
            group_by: 聚合维度，取值为 ``model`` / ``thread`` / ``day``。

        Returns:
            形如 ``{"prompt_tokens": int, "completion_tokens": int,
            "total_tokens": int, "run_count": int, "groups": [{"key": str,
            "prompt_tokens": int, "completion_tokens": int,
            "total_tokens": int, "run_count": int}]}`` 的字典。

        Raises:
            ValueError: 参数非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        column = _GROUP_COLUMNS.get(group_by)
        if column is None:
            raise ValueError(f"group_by 必须是 {sorted(_GROUP_COLUMNS)} 之一，实际：{group_by}")

        conditions: list[str] = []
        params: list[Any] = []
        if owner_id is not None:
            conditions.append("owner_id = ?")
            params.append(owner_id)
        if thread_id is not None:
            conditions.append("thread_id = ?")
            params.append(thread_id)
        if since is not None:
            if not isinstance(since, str) or not since.strip():
                raise ValueError("since 必须是非空字符串")
            conditions.append("created_at >= ?")
            params.append(since)
        if until is not None:
            if not isinstance(until, str) or not until.strip():
                raise ValueError("until 必须是非空字符串")
            conditions.append("created_at < ?")
            params.append(until)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        async with self._lock:
            try:
                async with self._conn.execute(
                    f"""
                    SELECT
                        COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                        COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                        COUNT(1) AS run_count
                    FROM usage_log
                    {where}
                    """,
                    tuple(params),
                ) as cursor:
                    totals = await cursor.fetchone()

                async with self._conn.execute(
                    f"""
                    SELECT
                        {column} AS group_key,
                        COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                        COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                        COUNT(1) AS run_count
                    FROM usage_log
                    {where}
                    GROUP BY group_key
                    ORDER BY (SUM(prompt_tokens) + SUM(completion_tokens)) DESC, group_key ASC
                    """,
                    tuple(params),
                ) as cursor:
                    rows = await cursor.fetchall()
            except Exception:
                logger.exception("用量聚合查询失败：group_by=%s", group_by)
                raise

        prompt_total = int(totals["prompt_tokens"]) if totals is not None else 0
        completion_total = int(totals["completion_tokens"]) if totals is not None else 0
        run_count = int(totals["run_count"]) if totals is not None else 0

        groups = [
            {
                "key": "" if row["group_key"] is None else str(row["group_key"]),
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
                "total_tokens": int(row["prompt_tokens"]) + int(row["completion_tokens"]),
                "run_count": int(row["run_count"]),
            }
            for row in rows
        ]

        logger.debug(
            "用量聚合完成：group_by=%s run_count=%d total=%d",
            group_by,
            run_count,
            prompt_total + completion_total,
        )
        return {
            "prompt_tokens": prompt_total,
            "completion_tokens": completion_total,
            "total_tokens": prompt_total + completion_total,
            "run_count": run_count,
            "groups": groups,
        }

    async def count_all(self) -> int:
        """返回用量记录总数（运维与测试用）。"""
        async with self._lock:
            try:
                async with self._conn.execute("SELECT COUNT(1) AS total FROM usage_log") as cursor:
                    row = await cursor.fetchone()
            except Exception:
                logger.exception("统计用量记录条数失败")
                raise
        return int(row["total"]) if row is not None else 0

    # ------------------------------------------------------------------ 校验

    @staticmethod
    def _validate_thread_id(thread_id: str) -> str:
        if not isinstance(thread_id, str):
            raise ValueError(f"thread_id 必须是字符串，实际：{type(thread_id).__name__}")
        normalized = thread_id.strip()
        if not normalized:
            raise ValueError("thread_id 不能为空")
        if len(normalized) > 128:
            raise ValueError("thread_id 过长")
        return normalized

    @staticmethod
    def _validate_model(model: str) -> str:
        if not isinstance(model, str):
            raise ValueError(f"model 必须是字符串，实际：{type(model).__name__}")
        normalized = model.strip()
        if len(normalized) > MAX_MODEL_CHARS:
            raise ValueError(f"model 过长（{len(normalized)} > {MAX_MODEL_CHARS}）")
        return normalized

    @staticmethod
    def _validate_owner(owner_id: str) -> str:
        if owner_id is None:
            return ""
        if not isinstance(owner_id, str):
            raise ValueError(f"owner_id 必须是字符串，实际：{type(owner_id).__name__}")
        normalized = owner_id.strip()
        if len(normalized) > _MAX_OWNER_CHARS:
            raise ValueError(f"owner_id 过长（{len(normalized)} > {_MAX_OWNER_CHARS}）")
        return normalized

    @staticmethod
    def _validate_tokens(field: str, value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{field} 必须是整数，实际：{type(value).__name__}")
        if value < 0:
            raise ValueError(f"{field} 不能为负数，实际：{value}")
        if value > _MAX_TOKEN_VALUE:
            raise ValueError(f"{field} 超出上限（{value} > {_MAX_TOKEN_VALUE}）")
        return value


@asynccontextmanager
async def open_usage_store(db_path: Path) -> AsyncIterator[UsageStore]:
    """以异步上下文的方式提供用量存储，退出时关闭连接。

    Args:
        db_path: SQLite 文件路径，父目录会自动创建。

    Yields:
        已完成建表的 ``UsageStore``。

    Raises:
        ValueError: ``db_path`` 为 ``None``。
        aiosqlite.Error: 建表失败时原样向上抛出。
    """
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
            # 而升级前的库里已有用量数据。重复执行必然抛「列已存在」，忽略即可。
            await conn.execute("ALTER TABLE usage_log ADD COLUMN trace_id TEXT;")
        except Exception:
            logger.debug("usage_log.trace_id 已存在，跳过迁移")
        # WHY 索引必须排在补列之后：它是列上建的，顺序反了会让老库启动即失败。
        await conn.executescript(_TRACE_INDEX)
        await conn.commit()
        logger.info("用量记录表已就绪：%s", db_path)
        yield UsageStore(conn)
    except Exception:
        logger.exception("用量记录表初始化失败：%s", db_path)
        raise
    finally:
        if conn is not None:
            await conn.close()
            logger.info("用量记录连接已关闭：%s", db_path)


__all__ = ["MAX_MODEL_CHARS", "UsageStore", "open_usage_store", "utc_now", "window_start"]
