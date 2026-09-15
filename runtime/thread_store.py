"""会话元数据存储。

WHY 需要一张独立的表：检查点表（``checkpoints``）只保存图状态，其中的
``metadata`` 是 LangGraph 自用字段（``source`` / ``step`` / ``writes`` / ``parents``），
既没有标题也没有时间戳，而会话列表恰恰需要这两项。靠反序列化 BLOB 来凑列表
成本极高（每条会话都要解包 msgpack），因此另建一张窄表，
并与检查点共用同一个 SQLite 文件，避免引入第二个数据库实例。

WHY 不引入 ORM：本项目的持久化栈就是 SQLite + aiosqlite——检查点由
``AsyncSqliteSaver`` 使用同一驱动、同一文件。新增 ORM 只会带来第二套连接生命周期
与事务语义，违背「数据库操作复用既有访问方式」的约定。

并发模型：SQLite 本身是单写多读。WAL 模式（由检查点侧开启，是库级持久属性）
允许本表与检查点并行读写；``busy_timeout`` 让写锁冲突等待而非立即失败。
连接对象本身不是并发安全的，因此所有语句都在一把 ``asyncio.Lock`` 下执行。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiosqlite

from text_utils import build_title

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# WHY 硬上限放在存储层：即便调用方漏做校验，也不允许超长文本灌进数据库。
MAX_THREAD_ID_CHARS = 128
_MAX_TITLE_CHARS = 200
_MAX_LIMIT = 200
_MAX_TURN_DELTA = 100


def normalize_thread_id(thread_id: str) -> str:
    """校验会话 ID 并返回规范化结果。

    WHY 做成模块级公开函数：会话 ID 的合法性校验此前在路由层、服务层与存储层
    各写了一遍，三处的规则（是否 strip、长度上限多少、非字符串如何处理）随时
    可能漂移。这里作为唯一实现，上层只负责把 ``ValueError`` 转成各自的语义
    （HTTP 400 / 事件流错误帧）。

    Args:
        thread_id: 待校验的会话 ID。

    Returns:
        去除首尾空白后的会话 ID。

    Raises:
        ValueError: 非字符串、为空或超出长度上限。
    """
    if not isinstance(thread_id, str):
        raise ValueError(f"thread_id 必须是字符串，实际：{type(thread_id).__name__}")
    normalized = thread_id.strip()
    if not normalized:
        raise ValueError("thread_id 不能为空")
    if len(normalized) > MAX_THREAD_ID_CHARS:
        raise ValueError(
            f"thread_id 过长（{len(normalized)} > {MAX_THREAD_ID_CHARS}）"
        )
    return normalized

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_meta (
    thread_id     TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    turn_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_thread_meta_updated_at
    ON thread_meta (updated_at DESC, thread_id DESC);
"""

_COLUMNS = "thread_id, title, created_at, updated_at, turn_count"


def _utc_now() -> str:
    """返回可直接按字典序比较的 ISO8601 UTC 时间串。

    WHY 不用 SQLite 的 ``datetime('now')``：把时间格式的决定权留在应用层，
    格式固定为「同偏移、定宽」后，字符串排序即等价于时间排序，
    排序可以完全交给索引而无需任何转换函数。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_title(title: str | None) -> str:
    """压缩标题中的空白并截断到存储层硬上限。

    WHY 复用 ``text_utils.build_title`` 而不是各写一份：折叠与截断的规则必须与
    应用层完全一致，否则会出现「列表页显示的标题与库中存的不是同一个」。
    两层的区别只是阈值——应用层按展示宽度截，存储层按入库硬上限截，
    因此阈值作为参数传入。
    """
    return build_title(title, _MAX_TITLE_CHARS)


class ThreadMetaStore:
    """会话元数据的读写门面。

    本类只做「存取」，不承载任何业务规则（标题取多长、轮次怎么算由应用层决定），
    这样存储层的语义可以被稳定地测试与复用。
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        if conn is None:
            raise ValueError("conn 不能为 None")

        self._conn = conn
        # WHY 用单一锁而非按 thread_id 分锁：本表体量极小（一行一会话），
        # 分锁带来的复杂度远大于收益；而 aiosqlite 连接不允许并发语句交错。
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 校验

    @staticmethod
    def _validate_thread_id(thread_id: str) -> str:
        """校验会话 ID 并返回规范化结果。

        Raises:
            ValueError: 为空、非字符串或超出长度上限。
        """
        return normalize_thread_id(thread_id)

    @staticmethod
    def _validate_paging(limit: int, offset: int) -> None:
        """校验分页参数。

        Raises:
            ValueError: ``limit`` 不在 1.._MAX_LIMIT 内，或 ``offset`` 为负数。
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError(f"limit 必须是整数，实际：{type(limit).__name__}")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValueError(f"offset 必须是整数，实际：{type(offset).__name__}")
        if limit < 1 or limit > _MAX_LIMIT:
            raise ValueError(f"limit 必须在 1..{_MAX_LIMIT} 之间，实际：{limit}")
        if offset < 0:
            raise ValueError(f"offset 不能为负数，实际：{offset}")

    # ------------------------------------------------------------------ 写入

    async def create(self, thread_id: str, *, title: str = "") -> dict[str, Any]:
        """登记一个新会话；已存在时保持原记录不变（幂等）。

        Args:
            thread_id: 会话 ID。
            title: 初始标题，空串表示尚未命名。

        Returns:
            该会话的完整元数据字典。

        Raises:
            ValueError: 参数非法。
            RuntimeError: 写入成功但随后读取不到记录（说明库被外部改动）。
            aiosqlite.Error: 数据库层异常，原样向上抛出由调用方决定是否重试。
        """
        normalized_id = self._validate_thread_id(thread_id)
        normalized_title = _normalize_title(title)
        now = _utc_now()

        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    INSERT INTO thread_meta (thread_id, title, created_at, updated_at, turn_count)
                    VALUES (?, ?, ?, ?, 0)
                    ON CONFLICT(thread_id) DO NOTHING
                    """,
                    (normalized_id, normalized_title, now, now),
                ) as cursor:
                    inserted = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception("登记会话失败：thread=%s", normalized_id)
                raise

        if not inserted:
            logger.warning("会话已登记，保持原记录：thread=%s", normalized_id)
        else:
            logger.info("会话已登记：thread=%s", normalized_id)

        record = await self.get(normalized_id)
        if record is None:
            # WHY 这里必须炸：INSERT 返回成功后却读不到，说明库被外部进程改写，
            # 静默返回空数据会让上层以为「会话不存在」，从而走向错误的删除路径。
            raise RuntimeError(f"会话登记后读取失败：thread={normalized_id}")
        return record

    async def record_turn(
        self,
        thread_id: str,
        *,
        title_hint: str | None = None,
        turn_delta: int = 1,
    ) -> dict[str, Any] | None:
        """记录一轮对话：刷新活动时间、累加轮次，并在标题为空时补写标题。

        WHY 用单条 UPSERT 完成三件事：高频路径上减少一次往返与一次加锁窗口；
        同时保证「未登记过的会话」（例如历史遗留数据）也能被自动补齐，
        而不是在列表里凭空消失。

        Args:
            thread_id: 会话 ID。
            title_hint: 用于生成标题的原始文本；为 ``None`` 时不改动标题。
            turn_delta: 本轮新增的对话轮次，恢复执行传 0（同一次运行的延续）。

        Returns:
            更新后的元数据；``None`` 表示该会话此前未登记且本次未能写入。

        Raises:
            ValueError: 参数非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized_id = self._validate_thread_id(thread_id)
        if not isinstance(turn_delta, int) or isinstance(turn_delta, bool):
            raise ValueError(f"turn_delta 必须是整数，实际：{type(turn_delta).__name__}")
        if turn_delta < 0 or turn_delta > _MAX_TURN_DELTA:
            raise ValueError(f"turn_delta 必须在 0..{_MAX_TURN_DELTA} 之间，实际：{turn_delta}")

        normalized_title = _normalize_title(title_hint)
        now = _utc_now()

        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    INSERT INTO thread_meta (thread_id, title, created_at, updated_at, turn_count)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        turn_count = thread_meta.turn_count + ?,
                        title = CASE
                            WHEN thread_meta.title = '' THEN excluded.title
                            ELSE thread_meta.title
                        END
                    """,
                    (
                        normalized_id,
                        normalized_title,
                        now,
                        now,
                        turn_delta,
                        turn_delta,
                    ),
                ) as cursor:
                    changed = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception("记录会话活动失败：thread=%s", normalized_id)
                raise

        if not changed:
            logger.warning("记录会话活动未命中任何行：thread=%s", normalized_id)
            return None

        return await self.get(normalized_id)

    async def touch(self, thread_id: str) -> bool:
        """只刷新最近活动时间，不改变标题与轮次。

        WHY 单独一个方法而不是复用 ``record_turn(turn_delta=0)``：一轮运行会在
        开始与结束各刷新一次时间，用 UPSERT 做纯时间刷新会连带执行
        ``turn_count + 0`` 与标题的 CASE 判断，多一次无意义的写放大与锁窗口。

        Args:
            thread_id: 会话 ID。

        Returns:
            是否命中并更新了一行；``False`` 表示该会话尚未登记。

        Raises:
            ValueError: ``thread_id`` 非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized_id = self._validate_thread_id(thread_id)

        async with self._lock:
            try:
                async with self._conn.execute(
                    "UPDATE thread_meta SET updated_at = ? WHERE thread_id = ?",
                    (_utc_now(), normalized_id),
                ) as cursor:
                    updated = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception("刷新会话活动时间失败：thread=%s", normalized_id)
                raise

        if not updated:
            logger.debug("刷新活动时间未命中任何行：thread=%s", normalized_id)
        return updated

    async def delete(self, thread_id: str) -> bool:
        """删除会话元数据。

        Returns:
            ``True`` 表示确实删掉了一行；``False`` 表示原本就没有该会话。

        Raises:
            ValueError: ``thread_id`` 非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized_id = self._validate_thread_id(thread_id)

        async with self._lock:
            try:
                async with self._conn.execute(
                    "DELETE FROM thread_meta WHERE thread_id = ?", (normalized_id,)
                ) as cursor:
                    deleted = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception("删除会话元数据失败：thread=%s", normalized_id)
                raise

        if deleted:
            logger.info("会话元数据已删除：thread=%s", normalized_id)
        else:
            logger.warning("会话元数据不存在，无需删除：thread=%s", normalized_id)
        return deleted

    # ------------------------------------------------------------------ 读取

    async def get(self, thread_id: str) -> dict[str, Any] | None:
        """按 ID 读取单条元数据。

        Returns:
            元数据字典；不存在时返回 ``None``。

        Raises:
            ValueError: ``thread_id`` 非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized_id = self._validate_thread_id(thread_id)

        async with self._lock:
            try:
                async with self._conn.execute(
                    f"SELECT {_COLUMNS} FROM thread_meta WHERE thread_id = ?",
                    (normalized_id,),
                ) as cursor:
                    row = await cursor.fetchone()
            except Exception:
                logger.exception("读取会话元数据失败：thread=%s", normalized_id)
                raise

        return dict(row) if row is not None else None

    async def list_threads(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """按最近活动时间倒序列出会话。

        Args:
            limit: 返回条数，1..200。
            offset: 跳过的条数，用于分页。

        Returns:
            元数据字典列表，最近活动的在前。

        Raises:
            ValueError: 分页参数非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        self._validate_paging(limit, offset)

        async with self._lock:
            try:
                async with self._conn.execute(
                    f"""
                    SELECT {_COLUMNS} FROM thread_meta
                    ORDER BY updated_at DESC, thread_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ) as cursor:
                    rows = await cursor.fetchall()
            except Exception:
                logger.exception("查询会话列表失败：limit=%s offset=%s", limit, offset)
                raise

        logger.debug("会话列表查询完成：返回 %d 条（offset=%s）", len(rows), offset)
        return [dict(row) for row in rows]

    async def count(self) -> int:
        """返回会话总数，用于分页元信息。

        Raises:
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        async with self._lock:
            try:
                async with self._conn.execute(
                    "SELECT COUNT(1) FROM thread_meta"
                ) as cursor:
                    row = await cursor.fetchone()
            except Exception:
                logger.exception("统计会话总数失败")
                raise

        if row is None:
            # WHY 返回 0 而非报错：COUNT 查询无结果行在 SQLite 中不应发生，
            # 真要发生也只影响分页展示，不值得让整个列表接口失败。
            logger.warning("会话总数查询未返回结果行，按 0 处理")
            return 0
        return int(row[0])


@asynccontextmanager
async def open_thread_store(db_path: Path) -> AsyncIterator[ThreadMetaStore]:
    """以异步上下文的方式提供会话元数据存储，退出时关闭连接。

    WHY 与检查点共用同一个 ``db_path``：两者描述的是同一批会话，
    分库会带来「删了会话却删不掉元数据」这类跨库不一致问题。

    Args:
        db_path: SQLite 文件路径，父目录会自动创建。

    Yields:
        已完成建表的 ``ThreadMetaStore``。

    Raises:
        ValueError: ``db_path`` 为 ``None``。
        aiosqlite.Error: 建表或 PRAGMA 设置失败时原样向上抛出。
    """
    if db_path is None:
        raise ValueError("db_path 不能为 None")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn: aiosqlite.Connection | None = None

    try:
        conn = await aiosqlite.connect(str(db_path))
        # WHY 设为 Row：让 fetchone/fetchall 直接可按列名取值，
        # 避免下游用魔法下标（row[3]）读字段，字段顺序一变就会静默错位。
        conn.row_factory = aiosqlite.Row

        # WHY 重复设置 WAL：它是库级持久属性、通常已由检查点侧开启，
        # 但本模块不应假设初始化顺序，显式声明才能保证独立启用时行为一致。
        await conn.execute("PRAGMA journal_mode=WAL;")
        # WHY busy_timeout：检查点写入频繁，与本表写入可能同时发生；
        # 默认行为是立即返回 "database is locked"，等待几秒远比报错合理。
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.executescript(_SCHEMA)
        await conn.commit()

        logger.info("会话元数据表已就绪：%s", db_path)
        yield ThreadMetaStore(conn)
    except Exception:
        logger.exception("会话元数据表初始化失败：%s", db_path)
        raise
    finally:
        if conn is not None:
            await conn.close()
            logger.info("会话元数据连接已关闭：%s", db_path)
