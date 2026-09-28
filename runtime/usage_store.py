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

from runtime.sqlite_lifecycle import open_sqlite_store
from thread_utils import normalize_thread_id

logger = logging.getLogger(__name__)

MAX_MODEL_CHARS = 128
_MAX_OWNER_CHARS = 128
_MAX_TOKEN_VALUE = 100_000_000
"""单个计数的硬上限。

WHY 需要一个上限：provider 侧异常时可能返回荒谬的数值（例如把字节数当
token 数），写进库后所有聚合都会失真；上限拦不住故障，但能让故障可见。
"""

_MAX_LIST_ITEMS = 200
"""逐条查询一次最多返回的行数。

WHY 需要上限：逐条结果会原样序列化进 HTTP 响应，无上限时一次「看看最近调用」
就能拉回整表——既是响应体风险，也把同一数据库上的会话读写拖慢。200 条足以看清
一条会话的趋势（该会话量的调用次数远不到这个数量级）。
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
    cache_hit_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    trace_id          TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_log_owner_time
    ON usage_log (owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_usage_log_thread_time
    ON usage_log (thread_id, created_at DESC);
"""

_ADDED_COLUMNS: tuple[str, ...] = (
    "trace_id TEXT",
    "cache_hit_tokens INTEGER NOT NULL DEFAULT 0",
    "cache_miss_tokens INTEGER NOT NULL DEFAULT 0",
)
"""升级前的老库需要补的列（形如 ``<列名> <类型> <约束>``）。

WHY 与 ``_SCHEMA`` 并存而不是从它解析：建表脚本负责**新库**，这里负责**老库**，
两处必须给出同一组列。放在一处写死是为了让「再加一列时该改哪两行」是可见的
——分开写迟早漂开，而那表现为「新库有列、老库没有」，报错落在某条 SELECT 上，
与「谁漏了补列」看不出关系。
"""

_CACHE_HIT_COLUMN = "cache_hit_tokens"
_CACHE_MISS_COLUMN = "cache_miss_tokens"
"""缓存计数的列名；聚合与写入共用，避免两处各写一遍字面量。"""

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
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
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
            cache_hit_tokens: 输入中命中缓存的 token 数；provider 未上报时为 0。
            cache_miss_tokens: 输入中未命中缓存的 token 数；provider 未上报时为 0。
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
        cache_hit = self._validate_tokens("cache_hit_tokens", cache_hit_tokens)
        cache_miss = self._validate_tokens("cache_miss_tokens", cache_miss_tokens)
        timestamp = created_at if isinstance(created_at, str) and created_at.strip() else utc_now()

        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    INSERT INTO usage_log
                        (thread_id, owner_id, model, prompt_tokens, completion_tokens,
                         cache_hit_tokens, cache_miss_tokens, trace_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_thread,
                        normalized_owner,
                        normalized_model,
                        prompt,
                        completion,
                        cache_hit,
                        cache_miss,
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
            "total_tokens": int, "cache_hit_tokens": int,
            "cache_miss_tokens": int, "call_count": int, "groups": [{"key": str,
            "prompt_tokens": int, "completion_tokens": int,
            "total_tokens": int, "cache_hit_tokens": int,
            "cache_miss_tokens": int, "call_count": int}]}`` 的字典。

        Raises:
            ValueError: 参数非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        column = _GROUP_COLUMNS.get(group_by)
        if column is None:
            raise ValueError(f"group_by 必须是 {sorted(_GROUP_COLUMNS)} 之一，实际：{group_by}")

        where, params = self._build_filters(
            owner_id=owner_id, thread_id=thread_id, since=since, until=until
        )

        async with self._lock:
            try:
                async with self._conn.execute(
                    f"""
                    SELECT
                        COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                        COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                        COALESCE(SUM({_CACHE_HIT_COLUMN}), 0) AS cache_hit_tokens,
                        COALESCE(SUM({_CACHE_MISS_COLUMN}), 0) AS cache_miss_tokens,
                        COUNT(1) AS call_count
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
                        COALESCE(SUM({_CACHE_HIT_COLUMN}), 0) AS cache_hit_tokens,
                        COALESCE(SUM({_CACHE_MISS_COLUMN}), 0) AS cache_miss_tokens,
                        COUNT(1) AS call_count
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
        cache_hit_total = int(totals["cache_hit_tokens"]) if totals is not None else 0
        cache_miss_total = int(totals["cache_miss_tokens"]) if totals is not None else 0
        call_count = int(totals["call_count"]) if totals is not None else 0

        groups = [
            {
                "key": "" if row["group_key"] is None else str(row["group_key"]),
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
                "total_tokens": int(row["prompt_tokens"]) + int(row["completion_tokens"]),
                "cache_hit_tokens": int(row["cache_hit_tokens"]),
                "cache_miss_tokens": int(row["cache_miss_tokens"]),
                "call_count": int(row["call_count"]),
            }
            for row in rows
        ]

        logger.debug(
            "用量聚合完成：group_by=%s call_count=%d total=%d cache_hit=%d cache_miss=%d",
            group_by,
            call_count,
            prompt_total + completion_total,
            cache_hit_total,
            cache_miss_total,
        )
        return {
            "prompt_tokens": prompt_total,
            "completion_tokens": completion_total,
            "total_tokens": prompt_total + completion_total,
            "cache_hit_tokens": cache_hit_total,
            "cache_miss_tokens": cache_miss_total,
            "call_count": call_count,
            "groups": groups,
        }

    async def list_recent(
        self,
        *,
        owner_id: str | None = None,
        thread_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], bool]:
        """按**时间正序**返回最近若干条用量记录。

        WHY 与 ``summarize`` 分开而不是给它加一个 ``group_by=None``：两者返回值
        形状不同（聚合是若干档、逐条是一串记录），塞进同一个返回类型后，调用方每次
        都要先判断「这次拿到的是哪种」；分开之后类型本身就是文档。

        WHY 内部先倒序取再反转：要的是「最近的 N 条」，而只有 ``ORDER BY id DESC
        LIMIT`` 能借助主键索引只扫 N 行；若直接正序 LIMIT，拿到的是**最早**的 N 条
        ——那恰好是趋势的反面。

        WHY 多取一条：用「是否取到 limit + 1 条」判断有没有被截断。仅凭
        ``len(items) == limit`` 无法区分「刚好这么多」与「还有更早的」，而把后者
        当成前者，会让用户以为趋势就是从头开始的。

        Args:
            owner_id: 只取该所有者的记录；``None`` 表示不限制。
            thread_id: 只取该会话的记录；``None`` 表示不限制。
            since: 时间窗起点（含），``None`` 表示不限起点。
            until: 时间窗终点（不含），``None`` 表示不限终点。
            limit: 最多返回几条，取值 ``1.._MAX_LIST_ITEMS``。

        Returns:
            ``(按时间正序的记录列表, 是否因 limit 截断)``；每条含
            ``id / thread_id / model / prompt_tokens / completion_tokens /
            cache_hit_tokens / cache_miss_tokens / created_at``。

        Raises:
            ValueError: ``limit`` 或时间窗参数非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError(f"limit 必须是整数，实际：{type(limit).__name__}")
        if limit < 1 or limit > _MAX_LIST_ITEMS:
            raise ValueError(f"limit 必须在 1..{_MAX_LIST_ITEMS} 之间，实际：{limit}")

        where, params = self._build_filters(
            owner_id=owner_id, thread_id=thread_id, since=since, until=until
        )
        query_params = (*params, limit + 1)

        async with self._lock:
            try:
                async with self._conn.execute(
                    f"""
                    SELECT id, thread_id, model, prompt_tokens, completion_tokens,
                           {_CACHE_HIT_COLUMN} AS cache_hit_tokens,
                           {_CACHE_MISS_COLUMN} AS cache_miss_tokens,
                           created_at
                    FROM usage_log
                    {where}
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    query_params,
                ) as cursor:
                    rows = await cursor.fetchall()
            except Exception:
                logger.exception("用量逐条查询失败：thread=%s limit=%d", thread_id, limit)
                raise

        truncated = len(rows) > limit
        # 反转成时间正序：序列视角要回答的是「命中率随轮次怎么变」，左到右即先后
        recent = list(reversed(rows[:limit]))
        items = [
            {
                "id": int(row["id"]),
                "thread_id": str(row["thread_id"]),
                "model": str(row["model"]),
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
                "cache_hit_tokens": int(row["cache_hit_tokens"]),
                "cache_miss_tokens": int(row["cache_miss_tokens"]),
                "created_at": str(row["created_at"]),
            }
            for row in recent
        ]
        logger.debug(
            "用量逐条查询完成：thread=%s 返回 %d 条（截断=%s）",
            thread_id,
            len(items),
            truncated,
        )
        return items, truncated

    def _build_filters(
        self,
        *,
        owner_id: str | None,
        thread_id: str | None,
        since: str | None,
        until: str | None,
    ) -> tuple[str, list[Any]]:
        """构建 WHERE 子句与参数，供聚合与逐条查询共用。

        WHY 抽成一处：两个查询必须对「同一组过滤条件」给出完全一致的解释。分开写
        会让「按会话查聚合」与「按会话查序列」在边界上慢慢分叉，而那种不一致只在
        特定时间窗或特定会话下才暴露——两侧代码看起来都对。

        Returns:
            ``(WHERE 子句, 参数列表)``；没有任何过滤条件时子句是空串。

        Raises:
            ValueError: 时间窗参数不是非空字符串。
        """
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
        return where, params

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
        """校验会话 ID 并返回规范化结果。

        WHY 委托 ``thread_utils`` 而不是本模块自己判断：这条规则的权威实现在
        中立模块里（路由层、服务层、其它 store 都用它），长度上限也在那里。
        本模块曾把「非字符串 / 空 / 超过 128」重写了一遍——上限写成字面量，
        报错文案也不带具体长度；改上限时它会静默不跟，失效方式是
        「接口放行、用量入库被拒」这类只在特定长度下才暴露的错误。
        本方法保留下来只作为调用点的稳定名字，删掉它会牵动 ``record`` 的调用行。

        Raises:
            ValueError: 非字符串、为空或超出长度上限。
        """
        return normalize_thread_id(thread_id)

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


async def _prepare_usage_store(conn: aiosqlite.Connection) -> UsageStore:
    """建表、补列并返回存储门面；由 ``open_sqlite_store`` 在初始化阶段调用。"""
    await conn.executescript(_SCHEMA)
    for column in _ADDED_COLUMNS:
        # WHY 拼接而非参数化：``ALTER TABLE ADD COLUMN`` 的列名与类型在 SQLite 里
        # 不接受绑定参数，只能拼进语句；这里的取值全部来自模块内常量
        # ``_ADDED_COLUMNS``，不含任何外部输入，因此不构成注入面。
        try:
            # WHY 需要逐列迁移：``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表补列，
            # 而升级前的库里已有用量数据。已存在的列会抛「duplicate column name」，
            # 忽略即可；每条独立 try 才能让「只差其中一列」的老库也升上来。
            await conn.execute(f"ALTER TABLE usage_log ADD COLUMN {column};")
        except Exception:
            logger.debug("usage_log.%s 已存在，跳过迁移", column.split()[0])
    # WHY 索引必须排在补列之后：它是列上建的，顺序反了会让老库启动即失败。
    await conn.executescript(_TRACE_INDEX)
    await conn.commit()
    return UsageStore(conn)


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
    async with open_sqlite_store(
        db_path, label="用量记录表", prepare=_prepare_usage_store
    ) as store:
        logger.info("用量记录表已就绪：%s", db_path)
        yield store


__all__ = ["MAX_MODEL_CHARS", "UsageStore", "open_usage_store", "utc_now", "window_start"]
