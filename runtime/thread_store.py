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

from text_utils import build_title, collapse_whitespace
from thread_utils import normalize_thread_id

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_TITLE_CHARS = 200
_MAX_LIMIT = 200
_MAX_TURN_DELTA = 100


def normalize_search_query(query: str | None) -> str | None:
    """校验并归一标题搜索关键字。

    WHY 放在模块级并公开：与 ``thread_utils.normalize_thread_id`` 同理——搜索
    关键字的合法性只有一份定义，应用层（决定回 400 还是 500）与存储层（拼接
    LIKE 模式）各自调用，避免「服务层放行、存储层拒绝」这种口径不一致；也让
    服务层的校验不依赖「存储实现恰好也会校验」这一巧合。

    WHY 留在存储层而不随 ``normalize_thread_id`` 一起下沉：它的上限直接源自标题
    字段的入库硬上限（``_MAX_TITLE_CHARS``），存在理由是「被搜到的标题必落在 LIKE
    模式长度内」——这是存储查询的约束；且它只被应用层与存储层使用，接口层不碰，
    当前的依赖方向已合法，没有需要消除的门面。

    WHY 需要长度上限：搜索串会被拼进 LIKE 模式，超长输入除了无意义地扫描全表外
    没有任何作用；上限取与标题硬上限一致，保证能被搜到的标题一定在模式长度之内。

    Args:
        query: 原始关键字。

    Returns:
        归一后的关键字；``None`` 与空白串都表示「不过滤」。

    Raises:
        ValueError: 非字符串或超出长度上限。
    """
    if query is None:
        return None
    if not isinstance(query, str):
        raise ValueError(f"query 必须是字符串，实际：{type(query).__name__}")
    collapsed = collapse_whitespace(query)
    if not collapsed:
        return None
    if len(collapsed) > _MAX_TITLE_CHARS:
        raise ValueError(f"query 过长（{len(collapsed)} > {_MAX_TITLE_CHARS}）")
    return collapsed


# ``archived`` 是软删除标记（0/1），``archived_at`` 记录归档时刻、取消归档时清空。
# WHY 用软删除：删除会连带清掉检查点且不可恢复，而多数时候用户只是想让清单干净，
# 过一阵还想翻回来；真删除仍由 ``delete`` 提供。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_meta (
    thread_id     TEXT PRIMARY KEY,
    owner_id      TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    turn_count    INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0,
    archived_at   TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_thread_meta_updated_at
    ON thread_meta (updated_at DESC, thread_id DESC);
-- 分支表：一次分叉等于「把旧分支的头冻结下来 + 登记一个新分支」。
-- WHY 有必要自己存：上游检查点的头指针会跟着最新的一次运行走（切换分支的代价为零，
-- 但「有哪些分支」上游不给），也只认「按 id 取某个检查点」这一种读法。所以分支清单
-- 只能由我们自己维护——主键取 (thread_id, branch_id)，分支 id 因此是会话内唯一的。
CREATE TABLE IF NOT EXISTS thread_branches (
    thread_id         TEXT NOT NULL,
    branch_id         TEXT NOT NULL,
    head_checkpoint   TEXT NOT NULL DEFAULT '',
    parent_branch_id  TEXT NOT NULL DEFAULT '',
    origin            TEXT NOT NULL DEFAULT '',
    label             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    PRIMARY KEY (thread_id, branch_id)
);
CREATE INDEX IF NOT EXISTS idx_thread_branches_thread
    ON thread_branches (thread_id, created_at);
"""

_MIGRATIONS = [
    """
    ALTER TABLE thread_meta ADD COLUMN owner_id TEXT NOT NULL DEFAULT '';
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_thread_meta_owner_updated
        ON thread_meta (owner_id, updated_at DESC, thread_id DESC);
    """,
    # 归档列：老库升级路径。SQLite 的 ADD COLUMN 带默认值会为既有行填默认值，
    # 因此升级后所有历史会话都处于「未归档」，与升级前的可见性一致。
    """
    ALTER TABLE thread_meta ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
    """,
    """
    ALTER TABLE thread_meta ADD COLUMN archived_at TEXT NOT NULL DEFAULT '';
    """,
    # 列表默认过滤 archived = 0 并按 updated_at 排序，索引把过滤与排序一起覆盖，
    # 避免归档会话一多就退化成全表扫描 + 临时排序。
    """
    CREATE INDEX IF NOT EXISTS idx_thread_meta_archived_updated
        ON thread_meta (archived, updated_at DESC, thread_id DESC);
    """,
    # 当前分支游标：空串表示「根分支」。老库升级后为空串，与升级前「只有一条分支」
    # 的可见性完全一致，不需要额外回填。
    """
    ALTER TABLE thread_meta ADD COLUMN current_branch TEXT NOT NULL DEFAULT '';
    """,
    # 标签：以「前后都带逗号」的规范形式存一个字符串（见 normalize_tags）。
    # WHY 不建关联表：标签要按「会话」整体读写，单列足够表达；而过滤走 LIKE，
    # 关联表带来的收益（可索引）在 LIKE 模式下用不上，只多一层 JOIN 与生命周期管理。
    """
    ALTER TABLE thread_meta ADD COLUMN tags TEXT NOT NULL DEFAULT '';
    """,
]

_COLUMNS = (
    "thread_id, owner_id, title, created_at, updated_at, turn_count, "
    "archived, archived_at, current_branch, tags"
)

_MAX_TAG_CHARS = 32
"""单个标签的字符上限。"""

_MAX_TAGS = 10
"""一条会话允许的标签数量上限。

WHY 两个上限都要有：标签会整串存在一个字段里并参与 LIKE 过滤，无上限时
一个会话就能把这一列撑成正文——而它本来是给清单分类用的。
"""


def normalize_tags(tags: list[str] | None) -> list[str]:
    """规整标签列表：去空白、去重、校验长度与数量，并保持原顺序。

    WHY 放在存储层并公开：与 ``normalize_search_query`` 同理——标签的存储形式
    （规范串）与过滤模式（``%,tag,%``）都由本模块决定，规范化必须与它们同源，
    否则会出现「存进去的标签查不出来」这种只能靠猜的现象。

    WHY 拒绝含逗号的标签：存储形式用逗号分隔，含逗号的标签会直接把一个标签
    拆成两个——静默改变用户输入，且下次读出来才发现。

    Args:
        tags: 原始标签列表；``None`` 视为空列表。

    Returns:
        规范化后的标签列表（可能为空）。

    Raises:
        ValueError: 元素非字符串、含逗号、超长，或数量超限。
    """
    if tags is None:
        return []
    if not isinstance(tags, (list, tuple)):
        raise ValueError(f"tags 必须是列表，实际：{type(tags).__name__}")

    normalized: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError(f"标签必须是字符串，实际：{type(tag).__name__}")
        text = collapse_whitespace(tag)
        if not text:
            continue
        if "," in text:
            raise ValueError(f"标签不能含逗号：{text}")
        if len(text) > _MAX_TAG_CHARS:
            raise ValueError(f"标签过长（{len(text)} > {_MAX_TAG_CHARS}）：{text}")
        if text not in normalized:
            normalized.append(text)

    if len(normalized) > _MAX_TAGS:
        raise ValueError(f"标签过多（{len(normalized)} > {_MAX_TAGS}）")
    return normalized


def tags_to_storage(tags: list[str]) -> str:
    """把标签列表编成存储形式：前后各带一个逗号。

    WHY 前后都要逗号：这样 ``LIKE '%,tag,%'`` 匹配的是**完整**标签，
    ``tag`` 不会命中 ``mytag``——只靠单侧分隔符做不到这一点。
    """
    return f",{','.join(tags)}," if tags else ""


def tags_from_storage(raw: object) -> list[str]:
    """把存储形式解回标签列表。"""
    text = str(raw or "")
    return [part for part in text.split(",") if part]

_LIKE_ESCAPE = "\\"
"""LIKE 通配符的转义字符。"""


def _escape_like(text: str) -> str:
    """转义 LIKE 模式里的通配符。

    WHY 必须转义：``%`` 与 ``_`` 是 LIKE 的元字符。用户搜「50%」时若原样拼进
    模式串，会匹配到**所有**标题——现象是「搜索结果变多了」，极难联想到转义，
    因此这里连同转义符本身一起处理。
    """
    for char in (_LIKE_ESCAPE, "%", "_"):
        text = text.replace(char, _LIKE_ESCAPE + char)
    return text


def _build_filters(
    *,
    owner_id: str | None,
    include_unowned: bool,
    query: str | None,
    include_archived: bool,
    tag: str | None = None,
) -> tuple[str, list[Any]]:
    """把查询条件编译成 WHERE 子句与参数。

    WHY 抽成函数：``list_threads`` 与 ``count`` 必须用**完全相同**的过滤条件，
    否则分页元信息会与实际返回条数不符（表现为「还有下一页」但翻过去是空的）。
    此前 owner 条件已在两处各写一遍，归档与搜索再加进来就是四份。

    WHY 标签过滤也加在这里而不是另开一个编译点：同一条约束——过滤条件一旦有第二份
    实现，总数与条数就会在某个组合下分叉，而那种缺陷只在「翻页翻空」时暴露。
    """
    conditions: list[str] = []
    params: list[Any] = []

    # WHY 默认排除归档：归档的语义就是「从清单里收起来」，若默认仍返回，
    # 这个功能等于没做。
    if not include_archived:
        conditions.append("archived = 0")

    if tag:
        # 规范形式前后都带逗号，故模式两侧都要逗号；标签同样要转义，
        # 否则一个含 % 的标签会把整张表都匹配上。
        conditions.append(f"tags LIKE ? ESCAPE '{_LIKE_ESCAPE}'")
        params.append(f"%,{_escape_like(tag)},%")

    if owner_id is not None:
        if include_unowned:
            conditions.append("(owner_id = ? OR owner_id = '')")
        else:
            conditions.append("owner_id = ?")
        params.append(owner_id)

    if query:
        conditions.append(f"title LIKE ? ESCAPE '{_LIKE_ESCAPE}'")
        params.append(f"%{_escape_like(query)}%")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    return where, params


def _utc_now() -> str:
    """返回可直接按字典序比较的 ISO8601 UTC 时间串。

    WHY 不用 SQLite 的 ``datetime('now')``：把时间格式的决定权留在应用层，
    格式固定为「同偏移、定宽」后，字符串排序即等价于时间排序，
    排序可以完全交给索引而无需任何转换函数。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_record(row: Any) -> dict[str, Any]:
    """把数据库行转换成对外的元数据字典。

    WHY 单独做一次转换而不是直接 ``dict(row)``：``archived`` 在库里是 INTEGER
    （SQLite 没有布尔类型），直接把 0/1 透出去会让上层面对两种表示——
    而「真值判断」的写法在 0/1 下碰巧能工作，直到有人拿它做 ``is True`` 比较
    或序列化成 JSON 才暴露出来。
    """
    record = dict(row)
    record["archived"] = bool(record.get("archived"))
    # WHY 在这里解码标签：存储层对外的口径是「列表」，编解码只发生在读写这一刻；
    # 让上层自己 split 会把存储形式的细节泄漏到三个调用点，且每个都要记得处理空串。
    record["tags"] = tags_from_storage(record.get("tags"))
    return record


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

    async def create(
        self,
        thread_id: str,
        *,
        title: str = "",
        owner_id: str = "",
    ) -> dict[str, Any]:
        """登记一个新会话；已存在时保持原记录不变（幂等）。

        Args:
            thread_id: 会话 ID。
            title: 初始标题，空串表示尚未命名。
            owner_id: 会话所有者标识；认证关闭时为空串。

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
                    INSERT INTO thread_meta (thread_id, owner_id, title, created_at, updated_at, turn_count)
                    VALUES (?, ?, ?, ?, ?, 0)
                    ON CONFLICT(thread_id) DO NOTHING
                    """,
                    (normalized_id, owner_id, normalized_title, now, now),
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
        owner_id: str = "",
    ) -> dict[str, Any] | None:
        """记录一轮对话：刷新活动时间、累加轮次，并在标题为空时补写标题。

        WHY 用单条 UPSERT 完成三件事：高频路径上减少一次往返与一次加锁窗口；
        同时保证「未登记过的会话」（例如历史遗留数据）也能被自动补齐，
        而不是在列表里凭空消失。

        Args:
            thread_id: 会话 ID。
            title_hint: 用于生成标题的原始文本；为 ``None`` 时不改动标题。
            turn_delta: 本轮新增的对话轮次，恢复执行传 0（同一次运行的延续）。
            owner_id: 新建会话时的所有者；已存在会话不会被覆盖所有者。

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
                    INSERT INTO thread_meta (thread_id, owner_id, title, created_at, updated_at, turn_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        turn_count = thread_meta.turn_count + ?,
                        title = CASE
                            WHEN thread_meta.title = '' THEN excluded.title
                            ELSE thread_meta.title
                        END,
                        owner_id = CASE
                            WHEN thread_meta.owner_id = '' OR thread_meta.owner_id IS NULL
                                THEN excluded.owner_id
                            ELSE thread_meta.owner_id
                        END
                    """,
                    (
                        normalized_id,
                        owner_id,
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

    async def rename(self, thread_id: str, title: str) -> dict[str, Any] | None:
        """改写会话标题。

        WHY 不改 ``updated_at``：列表按「最近对话活动」排序，而改名不是对话活动。
        若改名刷新时间，用户整理一次标题就会把整个列表顺序打乱——那是纯粹的
        副作用，用户不会把顺序变化归因到「我刚改了标题」。

        WHY 超长标题报错而不是截断：截断会让「我输入的标题」与「列表里的标题」
        不一致，而这种差异只在标题很长时才出现，用户只会觉得界面在乱改他的输入。
        存储层的硬上限仍然保留，作为绕过服务层直接调用的兜底。

        Args:
            thread_id: 会话 ID。
            title: 新标题。

        Returns:
            更新后的元数据；``None`` 表示会话不存在（调用方应判定为 404）。

        Raises:
            ValueError: ``thread_id`` 非法、标题为空或超出长度上限。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized_id = self._validate_thread_id(thread_id)
        collapsed = collapse_whitespace(title)
        if not collapsed:
            raise ValueError("标题不能为空")
        if len(collapsed) > _MAX_TITLE_CHARS:
            raise ValueError(f"标题过长（{len(collapsed)} > {_MAX_TITLE_CHARS}）")

        async with self._lock:
            try:
                async with self._conn.execute(
                    "UPDATE thread_meta SET title = ? WHERE thread_id = ?",
                    (collapsed, normalized_id),
                ) as cursor:
                    updated = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception("改写会话标题失败：thread=%s", normalized_id)
                raise

        if not updated:
            logger.warning("改写标题未命中任何行：thread=%s", normalized_id)
            return None
        logger.info("会话标题已改写：thread=%s", normalized_id)
        return await self.get(normalized_id)

    async def set_archived(self, thread_id: str, archived: bool) -> dict[str, Any] | None:
        """设置会话的归档状态（软删除）。

        WHY 不改 ``updated_at``：理由同 ``rename`` ——归档是清单操作，不是对话活动。
        归档时刻记在 ``archived_at`` 里，取消归档时清空，避免留下「已恢复但仍有
        归档时间」这种自相矛盾的记录。

        Args:
            thread_id: 会话 ID。
            archived: ``True`` 归档，``False`` 恢复。

        Returns:
            更新后的元数据；``None`` 表示会话不存在（调用方应判定为 404）。

        Raises:
            ValueError: ``thread_id`` 非法或 ``archived`` 不是布尔值。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized_id = self._validate_thread_id(thread_id)
        if not isinstance(archived, bool):
            raise ValueError(f"archived 必须是布尔值，实际：{type(archived).__name__}")

        archived_at = _utc_now() if archived else ""
        async with self._lock:
            try:
                async with self._conn.execute(
                    "UPDATE thread_meta SET archived = ?, archived_at = ? WHERE thread_id = ?",
                    (1 if archived else 0, archived_at, normalized_id),
                ) as cursor:
                    updated = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception("设置会话归档状态失败：thread=%s", normalized_id)
                raise

        if not updated:
            logger.warning("设置归档状态未命中任何行：thread=%s", normalized_id)
            return None
        logger.info("会话归档状态已更新：thread=%s archived=%s", normalized_id, archived)
        return await self.get(normalized_id)

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

    # ------------------------------------------------------------------ 探测

    async def ping(self) -> bool:
        """探测数据库连通性。

        WHY 需要一条与业务无关的语句：就绪探测不能依赖任何业务表的存在与
        内容，否则「表还没建好」与「连接已断开」会混为一谈；``SELECT 1``
        只验证连接可用，是探测的最小充分条件。

        Returns:
            ``True`` 表示连接可用。

        Raises:
            RuntimeError: 查询未返回结果行（连接已不可用）。
            aiosqlite.Error: 数据库层异常，原样向上抛出，由调用方决定降级策略。
        """
        async with self._lock:
            try:
                async with self._conn.execute("SELECT 1") as cursor:
                    row = await cursor.fetchone()
            except Exception:
                logger.exception("数据库连通性探测失败")
                raise

        if row is None:
            # WHY 这里必须炸：连通性探测没有结果行意味着连接已失效，静默返回
            # True 会让就绪探针把不可用的实例放进负载均衡。
            raise RuntimeError("数据库连通性探测未返回结果行")
        return True

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

        return _row_to_record(row) if row is not None else None

    async def list_threads(
        self,
        *,
        owner_id: str | None = None,
        include_unowned: bool = False,
        limit: int = 50,
        offset: int = 0,
        query: str | None = None,
        tag: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """按最近活动时间倒序列出会话。

        Args:
            owner_id: 只返回该所有者的会话；``None`` 表示不限制。
            include_unowned: 为 ``True`` 时同时返回 ``owner_id=''`` 的会话，
                用于向后兼容与迁移场景。
            limit: 返回条数，1..200。
            offset: 跳过的条数，用于分页。
            query: 标题关键字；``None`` 或空串表示不过滤。**只搜标题**——
                消息正文存在检查点的 msgpack BLOB 里，检索需要逐条反序列化，
                那是另一个量级的成本（详见项目文档的已知限制）。
            include_archived: 是否连同已归档的会话一起返回。

        Returns:
            元数据字典列表，最近活动的在前。

        Raises:
            ValueError: 分页参数或 ``query`` 非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        self._validate_paging(limit, offset)
        normalized_query = normalize_search_query(query)

        where, params = _build_filters(
            owner_id=owner_id,
            include_unowned=include_unowned,
            query=normalized_query,
            include_archived=include_archived,
            tag=tag,
        )
        sql = f"""
            SELECT {_COLUMNS} FROM thread_meta
            {where}
            ORDER BY updated_at DESC, thread_id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        async with self._lock:
            try:
                async with self._conn.execute(sql, tuple(params)) as cursor:
                    rows = await cursor.fetchall()
            except Exception:
                logger.exception(
                    "查询会话列表失败：limit=%s offset=%s query=%s archived=%s",
                    limit,
                    offset,
                    normalized_query,
                    include_archived,
                )
                raise

        logger.debug(
            "会话列表查询完成：返回 %d 条（offset=%s query=%s）",
            len(rows),
            offset,
            normalized_query,
        )
        return [_row_to_record(row) for row in rows]

    async def count(
        self,
        *,
        owner_id: str | None = None,
        include_unowned: bool = False,
        query: str | None = None,
        tag: str | None = None,
        include_archived: bool = False,
    ) -> int:
        """返回会话总数，用于分页元信息。

        Args:
            owner_id: 只统计该所有者的会话；``None`` 表示不限制。
            include_unowned: 是否同时统计 ``owner_id=''`` 的会话。
            query: 标题关键字；必须与 ``list_threads`` 传同一个值，
                否则总数与实际返回条数不符。
            include_archived: 是否连同已归档的会话一起统计。

        Raises:
            ValueError: ``query`` 非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized_query = normalize_search_query(query)
        where, params = _build_filters(
            owner_id=owner_id,
            include_unowned=include_unowned,
            query=normalized_query,
            include_archived=include_archived,
            tag=tag,
        )
        sql = f"SELECT COUNT(1) FROM thread_meta {where}"

        async with self._lock:
            try:
                async with self._conn.execute(sql, tuple(params)) as cursor:
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


    async def set_tags(self, thread_id: str, tags: list[str] | None) -> dict[str, Any] | None:
        """整体替换某会话的标签。

        WHY 是「整体替换」而不是增删单个：界面上的标签是一次编辑后整体提交的，
        逐个增删需要前端自己算差集，而差集算错的表现是「删不掉的标签」。
        整体替换让服务端行为与用户看到的一致。

        WHY 不改 ``updated_at``：与 ``rename`` 同理——打标签是清单整理，不是对话活动，
        刷新时间会把会话清单的顺序搅乱。

        Args:
            thread_id: 会话 ID。
            tags: 新标签列表；``None`` 或空列表表示清空。

        Returns:
            更新后的元数据；``None`` 表示会话不存在（调用方应判定为 404）。

        Raises:
            ValueError: ``thread_id`` 非法或标签不合法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized_id = self._validate_thread_id(thread_id)
        normalized_tags = normalize_tags(tags)

        async with self._lock:
            try:
                async with self._conn.execute(
                    "UPDATE thread_meta SET tags = ? WHERE thread_id = ?",
                    (tags_to_storage(normalized_tags), normalized_id),
                ) as cursor:
                    updated = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception("设置会话标签失败：thread=%s", normalized_id)
                raise

        if not updated:
            logger.warning("设置标签未命中任何行：thread=%s", normalized_id)
            return None
        logger.info("会话标签已更新：thread=%s tags=%s", normalized_id, normalized_tags)
        return await self.get(normalized_id)

    async def set_branch_head(self, thread_id: str, branch_id: str, head_checkpoint: str) -> None:
        """冻结某条分支的头检查点；分支不存在时顺带把根分支补登记。

        WHY 只更新头而不整体 UPSERT：冻结发生在「即将离开这条分支」的时刻，这一动作
        只应改头，不该把既有的 origin / label / parent 覆盖掉——那些字段描述的是这条
        分支从哪来，与它此刻停在哪无关。

        Args:
            thread_id: 会话 ID。
            branch_id: 分支标识；空串表示根分支。
            head_checkpoint: 冻结下来的检查点 id。

        Raises:
            ValueError: ``thread_id`` 非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized_id = self._validate_thread_id(thread_id)
        async with self._lock:
            try:
                await self._conn.execute(
                    """
                    INSERT INTO thread_branches
                        (thread_id, branch_id, head_checkpoint, parent_branch_id,
                         origin, label, created_at)
                    VALUES (?, ?, ?, '', 'root', '', ?)
                    ON CONFLICT (thread_id, branch_id) DO UPDATE SET
                        head_checkpoint = excluded.head_checkpoint
                    """,
                    (normalized_id, branch_id, head_checkpoint, _utc_now()),
                )
                await self._conn.commit()
            except Exception:
                logger.exception(
                    "冻结分支头失败：thread=%s branch=%s", normalized_id, branch_id
                )
                raise

    async def upsert_branch(
        self,
        thread_id: str,
        branch_id: str,
        *,
        parent_branch_id: str = "",
        origin: str = "",
        label: str = "",
    ) -> dict[str, Any]:
        """登记（或更新）一条分支，头检查点留空表示「它就是当前分支」。

        WHY 用 UPSERT 而不是「已存在就报错」：同一轮分叉在重试时会被重复登记，
        幂等比把调用方逼去「先查再写」更好——后者中间正好是一个并发窗口。

        WHY 覆盖写时不动 ``created_at``：它是这条分支「从哪一刻起存在」的凭据，
        重试不该把它往后推，否则分支清单的排序会随重试次数漂移。

        Args:
            thread_id: 会话 ID。
            branch_id: 分支标识；空串表示根分支。
            parent_branch_id: 从哪条分支分叉而来。
            origin: 来源（root / edit / regenerate）。
            label: 界面展示用的简短说明。

        Returns:
            写入后的分支记录；仅当会话行不存在时可能为 ``None``。

        Raises:
            ValueError: ``thread_id`` 非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized_id = self._validate_thread_id(thread_id)
        async with self._lock:
            try:
                await self._conn.execute(
                    """
                    INSERT INTO thread_branches
                        (thread_id, branch_id, head_checkpoint, parent_branch_id,
                         origin, label, created_at)
                    VALUES (?, ?, '', ?, ?, ?, ?)
                    ON CONFLICT (thread_id, branch_id) DO UPDATE SET
                        parent_branch_id = excluded.parent_branch_id,
                        origin = excluded.origin,
                        label = excluded.label
                    """,
                    (normalized_id, branch_id, parent_branch_id, origin, label, _utc_now()),
                )
                await self._conn.commit()
            except Exception:
                logger.exception(
                    "登记分支失败：thread=%s branch=%s", normalized_id, branch_id
                )
                raise

        record = await self.get_branch(normalized_id, branch_id)
        if record is None:
            logger.warning("登记分支后回读未命中：thread=%s branch=%s", normalized_id, branch_id)
            return {}
        return record

    async def get_branch(self, thread_id: str, branch_id: str) -> dict[str, Any] | None:
        """读取一条分支记录；不存在返回 ``None``。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        normalized_id = self._validate_thread_id(thread_id)
        async with self._lock:
            async with self._conn.execute(
                """
                SELECT branch_id, head_checkpoint, parent_branch_id, origin, label, created_at
                FROM thread_branches WHERE thread_id = ? AND branch_id = ?
                """,
                (normalized_id, branch_id),
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def list_branches(self, thread_id: str) -> list[dict[str, Any]]:
        """列出某会话的全部分支，按创建时间升序（根分支在前的稳定顺序）。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        normalized_id = self._validate_thread_id(thread_id)
        async with self._lock:
            async with self._conn.execute(
                """
                SELECT branch_id, head_checkpoint, parent_branch_id, origin, label, created_at
                FROM thread_branches WHERE thread_id = ?
                ORDER BY created_at ASC, branch_id ASC
                """,
                (normalized_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def set_current_branch(self, thread_id: str, branch_id: str) -> bool:
        """把某条分支设为当前分支。

        WHY 当前分支必须落库而不是留在内存：它是「下一次运行接在哪条分支之后」的
        唯一依据，只存在进程里会让重启后接错分支——表现出来是「用户切了分支，
        回来一看回复接在了另一条上」。

        Args:
            thread_id: 会话 ID。
            branch_id: 分支标识；空串表示根分支。

        Returns:
            是否命中并更新了一行；``False`` 表示会话不存在。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        normalized_id = self._validate_thread_id(thread_id)
        async with self._lock:
            try:
                async with self._conn.execute(
                    "UPDATE thread_meta SET current_branch = ? WHERE thread_id = ?",
                    (branch_id, normalized_id),
                ) as cursor:
                    updated = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception("设置当前分支失败：thread=%s", normalized_id)
                raise

        if not updated:
            logger.warning("设置当前分支未命中任何行：thread=%s", normalized_id)
        return updated

    async def current_branch(self, thread_id: str) -> str:
        """读当前分支标识；空串表示根分支，会话不存在时同样返回空串。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        record = await self.get(thread_id)
        return (record or {}).get("current_branch", "") or ""


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
        for migration in _MIGRATIONS:
            try:
                await conn.executescript(migration)
            except Exception:
                # WHY 忽略重复迁移错误：SQLite 对已有列/索引的 ALTER 会抛错，
                # 但幂等迁移不需要回滚；非重复错误会在外层被记录。
                pass
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
