"""知识库存储：工作区文档的分块、关键词索引与向量索引。

三条检索路径共用一个库文件（探针实测：FTS5 虚拟表与 ``vec0`` 虚拟表可共存）：

- **向量检索**（``sqlite-vec``）：语义相近即可命中，与用词无关。需要嵌入后端。
- **关键词检索**（FTS5 + ``trigram``）：不需要嵌入，``none`` 档位下的唯一路径。
- **LIKE 回落**：``trigram`` 把文本切成三字符片段，**长度不足 3 的查询词一条都命中
  不了**（探针实测：「超时」「幂等」零命中，不是慢）。三字符以下改用 ``instr`` 子串
  匹配，在千级分块的量级上全表扫完全够用。

WHY 三张表而不是一张：正文只存一份（``knowledge_chunks``），两个索引表都指向它。
FTS5 用外部内容表（``content=''``的写法）而不是自带副本，是为了不让同一份正文在库里
出现两次——trigram 索引本身已经是原文的两倍量级。

WHY 向量表里冗余一列 ``owner_id``：跨主体隔离必须在 **SQL 里**完成。先取 top-k 再在
Python 里筛，会让「某个主体的命中恰好都被筛掉」时的返回条数少于 k，更糟的是筛之前
已经读到了别人的数据。探针确认 ``vec0`` 的 metadata 列过滤可用且不牺牲 top-k 正确性。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiosqlite

from text_utils import collapse_whitespace

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_MIN_FTS_QUERY_CHARS = 3
"""FTS5 ``trigram`` 能查询的最小片段长度。

WHY 3：``trigram`` 按三字符片段建索引，短于 3 的查询词无法构成任何一个片段，因此
**零命中而不是慢**（探针实测：「超时」「幂等」均为 0 命中）。这不是调参能解决的，
只能换成别的检索方式。
"""

_SCHEMA_VERSION = 1
"""库结构版本；结构变更时递增，并在 ``_MIGRATIONS`` 里补上升级路径。"""

_MAX_SOURCE_PATH_CHARS = 400
"""源文件路径的长度上限；与工作区相对路径的量级一致，超出必是调用方传错了值。"""

_MAX_QUERY_CHARS = 200
"""检索关键字的长度上限。

WHY 需要：查询串要么进 FTS 的 MATCH，要么进 ``instr`` 的全表扫；超长输入除了无意义地
扫描全表外没有任何作用。
"""

_MAX_LIMIT = 200
"""单次检索/列举的条数上限。"""


class KnowledgeStoreError(RuntimeError):
    """知识库存储层的配置或结构问题（区别于数据库本身的异常）。"""


@dataclass(frozen=True)
class ChunkInput:
    """待写入的一个分块。

    Attributes:
        ordinal: 该块在文档内的序号，从 0 开始；与 ``doc_id`` 一起唯一。
        heading: 该块所属的标题路径，用于结果里交代片段出处。
        body: 块正文。
    """

    ordinal: int
    heading: str
    body: str


@dataclass(frozen=True)
class KnowledgeHit:
    """一条检索命中。

    Attributes:
        chunk_id: 分块 ID。
        doc_id: 所属文档 ID。
        source_path: 源文件相对工作区的 POSIX 路径。
        ordinal: 块在文档内的序号。
        heading: 该块所属的标题路径。
        body: 块正文。
        score: 排序键，**越小越相关**。向量模式是 L2 距离；关键词模式是 ``bm25``
            （负值）；LIKE 回落模式是出现次数的相反数。
        mode: ``vector`` / ``keyword_fts`` / ``keyword_like``。

    Note:
        不同模式的 ``score`` 量纲不同、**不可互相比较**——需要合并两路结果时，应在
        上层按名次（如倒数排名融合）而不是按分数合并。
    """

    chunk_id: int
    doc_id: int
    source_path: str
    ordinal: int
    heading: str
    body: str
    score: float
    mode: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 一行一个已索引的源文件；(owner_id, source_path) 唯一，故重复索引同一文件是
-- 一次替换而不是新增。
CREATE TABLE IF NOT EXISTS knowledge_documents (
    doc_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     TEXT NOT NULL DEFAULT '',
    source_path  TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    indexed_at   TEXT NOT NULL,
    UNIQUE (owner_id, source_path)
);

-- 正文只存这一份；两个索引表都指向本表的 chunk_id。
-- owner_id 冗余在这里，是为了让关键词检索也能在 SQL 层完成隔离。
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id   INTEGER NOT NULL,
    owner_id TEXT NOT NULL DEFAULT '',
    ordinal  INTEGER NOT NULL,
    heading  TEXT NOT NULL DEFAULT '',
    body     TEXT NOT NULL,
    UNIQUE (doc_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_doc ON knowledge_chunks (doc_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_owner ON knowledge_chunks (owner_id);

-- 外部内容表：正文仍在 knowledge_chunks 里，本表只存 trigram 倒排。
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
    body,
    content='knowledge_chunks',
    content_rowid='chunk_id',
    tokenize='trigram'
);

-- 文档清单按主体 + 路径查，与检索一样需要走索引
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_owner
    ON knowledge_documents (owner_id, source_path);
"""

_META_KEYS = ("schema_version", "embedding_dims", "embedding_model")


def _utc_now() -> str:
    """返回可直接按字典序比较的 ISO8601 UTC 时间串（与 ``thread_store`` 同口径）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _vec_table_ddl(dims: int) -> str:
    """向量表建表语句。

    WHY 维度写死在 DDL 里：``vec0`` 的列宽在建表时即固定，这也是 ``EMBEDDING_DIMS``
    必须是配置项的原因。维度变了只能重建索引，不能在原表上改。
    """
    return f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_vec USING vec0(
        owner_id text,
        embedding float[{dims}]
    );
    """


def _pack_vector(vector: Sequence[float]) -> bytes:
    """把向量打包成 ``vec0`` 接受的 float32 小端字节串。

    Raises:
        ValueError: 元素数量不是 4 的倍数字节（此处只校验可打包性）。
    """
    import struct

    values = [float(item) for item in vector]
    return struct.pack(f"<{len(values)}f", *values)


class KnowledgeStore:
    """知识库的读写门面。

    本类只做「存取」，不做切分与嵌入——那两件事属于应用层（需要嵌入后端，而存储层
    按分层约定不能依赖 ``llm``）。因此调用方传入的已是切好的块与算好的向量。
    """

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        dims: int,
        model: str,
        vector_enabled: bool,
    ) -> None:
        """构造存储门面（请用 ``open_knowledge_store`` 而不是直接调用）。

        Args:
            conn: 已建表并加载扩展的连接。
            dims: 向量维度。
            model: 嵌入模型标识；用于识别「换了模型但维度没变」的情况。
            vector_enabled: 是否启用了向量检索。
        """
        self._conn = conn
        self._lock = asyncio.Lock()
        self.dims = dims
        self.model = model
        self.vector_enabled = vector_enabled

    # ------------------------------------------------------------------ 校验

    @staticmethod
    def _validate_owner(owner_id: str) -> str:
        """校验主体标识。

        Raises:
            ValueError: 非字符串。
        """
        if not isinstance(owner_id, str):
            raise ValueError(f"owner_id 必须是字符串，实际：{type(owner_id).__name__}")
        return owner_id

    @staticmethod
    def _validate_source_path(source_path: str) -> str:
        """校验并归一源文件路径。

        WHY 统一成 ``/`` 开头：本项目的虚拟路径约定就是带前导斜杠
        （``workspace_files.to_virtual_path`` 的返回值即 ``/a.md``）。去掉前导斜杠会
        让存储层的键与接口、工作区面板给出的路径差一个字符——调用方拿面板上的路径来
        删索引就会删不掉，而那看起来像「删除功能坏了」。

        WHY 同时把反斜杠归一：写入端若混进 Windows 原生分隔符，同一份文件会因写法
        不同被索引两次，表现为「重复文档」与「旧索引删不掉」。

        Raises:
            ValueError: 非字符串、为空、含 ``..`` 或超长。
        """
        if not isinstance(source_path, str):
            raise ValueError(f"source_path 必须是字符串，实际：{type(source_path).__name__}")
        candidate = source_path.strip().replace("\\", "/")
        while "//" in candidate:
            candidate = candidate.replace("//", "/")
        candidate = candidate.strip("/")
        if not candidate:
            raise ValueError("source_path 不能为空")
        if ".." in candidate.split("/"):
            raise ValueError(f"source_path 不能包含上跳片段：{source_path!r}")
        normalized = "/" + candidate
        if len(normalized) > _MAX_SOURCE_PATH_CHARS:
            raise ValueError(f"source_path 过长（{len(normalized)} > {_MAX_SOURCE_PATH_CHARS}）")
        return normalized

    @staticmethod
    def _validate_query(query: str) -> str:
        """校验并归一检索关键字。

        Raises:
            ValueError: 非字符串、为空或超长。
        """
        if not isinstance(query, str):
            raise ValueError(f"query 必须是字符串，实际：{type(query).__name__}")
        collapsed = collapse_whitespace(query)
        if not collapsed:
            raise ValueError("query 不能为空")
        if len(collapsed) > _MAX_QUERY_CHARS:
            raise ValueError(f"query 过长（{len(collapsed)} > {_MAX_QUERY_CHARS}）")
        return collapsed

    @staticmethod
    def _validate_limit(limit: int) -> int:
        """校验条数上限。

        Raises:
            ValueError: 不是 1..``_MAX_LIMIT`` 之间的整数。
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError(f"limit 必须是整数，实际：{type(limit).__name__}")
        if limit < 1 or limit > _MAX_LIMIT:
            raise ValueError(f"limit 必须在 1..{_MAX_LIMIT} 之间，实际：{limit}")
        return limit

    # ------------------------------------------------------------------ 写入

    async def replace_document(
        self,
        *,
        owner_id: str,
        source_path: str,
        content_hash: str,
        chunks: Sequence[ChunkInput],
        vectors: Sequence[Sequence[float]] | None = None,
    ) -> dict[str, Any]:
        """整体替换一份文档的索引（幂等）。

        WHY 是整体替换而不是增量更新：切分边界会随块大小等参数变化而移动，增量更新
        需要判断「哪些旧块还在新结果里」，而那个判断一旦出错留下的就是永不消失的
        幽灵片段。整体替换的代价只是重写一份文档的几十个块。

        WHY 必须在**一个事务**里完成：中途失败会留下「文档行说 10 块、实际只有 3 块」
        这种自相矛盾的状态，而检索会照常返回那 3 块，让人以为索引是完整的。

        Args:
            owner_id: 文档主体。
            source_path: 相对工作区的 POSIX 路径。
            content_hash: 内容指纹，用于跳过未变化的文档。
            chunks: 切好的分块，``ordinal`` 必须从 0 连续递增。
            vectors: 与 ``chunks`` 一一对应的向量；``None`` 表示不写向量。

        Returns:
            写入后的文档行（含 ``chunk_count``）。

        Raises:
            ValueError: 参数非法，或 ``vectors`` 与 ``chunks`` 数量/维度不符。
            KnowledgeStoreError: 未启用向量却传入了 ``vectors``。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        owner = self._validate_owner(owner_id)
        path = self._validate_source_path(source_path)
        if not isinstance(content_hash, str) or not content_hash.strip():
            raise ValueError("content_hash 不能为空")
        if not chunks:
            raise ValueError("chunks 不能为空；空文档不应进入索引")
        if vectors is not None:
            if not self.vector_enabled:
                raise KnowledgeStoreError(
                    "未启用向量检索（EMBEDDING_BACKEND=none），却传入了 vectors"
                )
            if len(vectors) != len(chunks):
                raise ValueError(
                    f"vectors 与 chunks 数量不符：{len(vectors)} != {len(chunks)}"
                )
            for index, vector in enumerate(vectors):
                if len(vector) != self.dims:
                    raise ValueError(
                        f"第 {index} 个向量维度不符：期望 {self.dims}，实际 {len(vector)}"
                    )

        ordinals = [chunk.ordinal for chunk in chunks]
        if ordinals != list(range(len(chunks))):
            raise ValueError(f"chunk.ordinal 必须从 0 连续递增，实际：{ordinals}")

        now = _utc_now()
        async with self._lock:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                await self._delete_document_rows(owner, path)
                async with self._conn.execute(
                    """
                    INSERT INTO knowledge_documents
                        (owner_id, source_path, content_hash, chunk_count, indexed_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (owner, path, content_hash, len(chunks), now),
                ) as cursor:
                    doc_id = int(cursor.lastrowid or 0)
                if doc_id <= 0:
                    raise RuntimeError(f"插入文档后未取得 doc_id：{path}")

                for chunk, vector in zip(chunks, vectors or [None] * len(chunks), strict=True):
                    async with self._conn.execute(
                        """
                        INSERT INTO knowledge_chunks (doc_id, owner_id, ordinal, heading, body)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (doc_id, owner, chunk.ordinal, chunk.heading, chunk.body),
                    ) as chunk_cursor:
                        chunk_id = int(chunk_cursor.lastrowid or 0)
                    if chunk_id <= 0:
                        raise RuntimeError(f"插入分块后未取得 chunk_id：{path}#{chunk.ordinal}")
                    # 外部内容表不随主表自动更新，必须显式写入同一 rowid
                    await self._conn.execute(
                        "INSERT INTO knowledge_chunks_fts (rowid, body) VALUES (?, ?)",
                        (chunk_id, chunk.body),
                    )
                    if vector is not None:
                        await self._conn.execute(
                            """
                            INSERT INTO knowledge_chunks_vec (rowid, owner_id, embedding)
                            VALUES (?, ?, ?)
                            """,
                            (chunk_id, owner, _pack_vector(vector)),
                        )

                await self._conn.execute("COMMIT")
            except Exception:
                # WHY 回滚要吞掉自身异常：ROLLBACK 失败（例如事务早已因错误结束）不该
                # 掩盖真正的失败原因，否则日志里只剩一条与根因无关的回滚报错。
                try:
                    await self._conn.execute("ROLLBACK")
                except Exception:
                    logger.debug("回滚索引事务失败（事务可能已结束）", exc_info=True)
                logger.exception("替换文档索引失败：owner=%s path=%s", owner, path)
                raise

        logger.info(
            "文档索引已替换：owner=%s path=%s chunks=%d vectors=%s",
            owner,
            path,
            len(chunks),
            "yes" if vectors is not None else "no",
        )
        record = await self.get_document(owner_id=owner, source_path=path)
        if record is None:
            # WHY 这里必须炸：刚提交成功却读不到，说明库被外部改动；静默返回空会让
            # 调用方以为索引成功落空，从而反复重试。
            raise RuntimeError(f"索引写入后读取失败：owner={owner} path={path}")
        return record

    async def _delete_document_rows(self, owner_id: str, source_path: str) -> None:
        """删除一份文档及其全部索引行（调用方必须已持有锁并处于事务中）。

        WHY 抽成私有方法：替换与删除两条路径都要做同一套清理，而其中 FTS 外部内容表
        的删除语法（``'delete'`` 伪命令）**必须带旧的正文**，漏掉任何一步都会留下
        检索得到、正文却已不在的幽灵命中。
        """
        async with self._conn.execute(
            "SELECT doc_id FROM knowledge_documents WHERE owner_id = ? AND source_path = ?",
            (owner_id, source_path),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return
        doc_id = int(row[0])

        # FTS 的删除必须给出被删行的正文，故在删除知识点行之前先执行
        await self._conn.execute(
            """
            INSERT INTO knowledge_chunks_fts (knowledge_chunks_fts, rowid, body)
            SELECT 'delete', chunk_id, body FROM knowledge_chunks WHERE doc_id = ?
            """,
            (doc_id,),
        )
        if self.vector_enabled:
            await self._conn.execute(
                """
                DELETE FROM knowledge_chunks_vec
                WHERE rowid IN (SELECT chunk_id FROM knowledge_chunks WHERE doc_id = ?)
                """,
                (doc_id,),
            )
        await self._conn.execute("DELETE FROM knowledge_chunks WHERE doc_id = ?", (doc_id,))
        await self._conn.execute("DELETE FROM knowledge_documents WHERE doc_id = ?", (doc_id,))

    async def delete_document(self, *, owner_id: str, source_path: str) -> bool:
        """删除一份文档的全部索引。

        Returns:
            ``True`` 表示确实删掉了一份文档；``False`` 表示原本就没有。

        Raises:
            ValueError: 参数非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        owner = self._validate_owner(owner_id)
        path = self._validate_source_path(source_path)

        async with self._lock:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                async with self._conn.execute(
                    "SELECT 1 FROM knowledge_documents WHERE owner_id = ? AND source_path = ?",
                    (owner, path),
                ) as cursor:
                    existed = await cursor.fetchone() is not None
                await self._delete_document_rows(owner, path)
                await self._conn.execute("COMMIT")
            except Exception:
                try:
                    await self._conn.execute("ROLLBACK")
                except Exception:
                    logger.debug("回滚删除事务失败（事务可能已结束）", exc_info=True)
                logger.exception("删除文档索引失败：owner=%s path=%s", owner, path)
                raise

        if existed:
            logger.info("文档索引已删除：owner=%s path=%s", owner, path)
        return existed

    # ------------------------------------------------------------------ 读取

    async def get_document(self, *, owner_id: str, source_path: str) -> dict[str, Any] | None:
        """读取一份文档的索引元数据；不存在返回 ``None``。"""
        owner = self._validate_owner(owner_id)
        path = self._validate_source_path(source_path)
        async with self._lock:
            async with self._conn.execute(
                """
                SELECT doc_id, owner_id, source_path, content_hash, chunk_count, indexed_at
                FROM knowledge_documents WHERE owner_id = ? AND source_path = ?
                """,
                (owner, path),
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def list_documents(self, *, owner_id: str, limit: int = _MAX_LIMIT) -> list[dict[str, Any]]:
        """列出某个主体已索引的文档，按路径排序。

        Raises:
            ValueError: 参数非法。
        """
        owner = self._validate_owner(owner_id)
        capped = self._validate_limit(limit)
        async with self._lock:
            async with self._conn.execute(
                """
                SELECT doc_id, source_path, content_hash, chunk_count, indexed_at
                FROM knowledge_documents WHERE owner_id = ?
                ORDER BY source_path ASC LIMIT ?
                """,
                (owner, capped),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def search_vector(
        self, *, owner_id: str, vector: Sequence[float], limit: int = 10
    ) -> list[KnowledgeHit]:
        """按向量检索最近的块。

        WHY 拆成两条语句而不是一条 JOIN：``vec0`` 的 KNN 查询对连接有限制，实测把
        ``MATCH ... ORDER BY distance`` 与普通表 JOIN 写在一起并不可靠。先取
        rowid + distance，再按 rowid 取正文，行为确定且足够快。

        Raises:
            ValueError: 参数非法或向量维度不符。
            KnowledgeStoreError: 未启用向量检索。
        """
        owner = self._validate_owner(owner_id)
        capped = self._validate_limit(limit)
        if not self.vector_enabled:
            raise KnowledgeStoreError("未启用向量检索（EMBEDDING_BACKEND=none）")
        if len(vector) != self.dims:
            raise ValueError(f"查询向量维度不符：期望 {self.dims}，实际 {len(vector)}")

        async with self._lock:
            async with self._conn.execute(
                """
                SELECT rowid, distance FROM knowledge_chunks_vec
                WHERE embedding MATCH ? AND owner_id = ?
                ORDER BY distance LIMIT ?
                """,
                (_pack_vector(vector), owner, capped),
            ) as cursor:
                ranked = [(int(row[0]), float(row[1])) for row in await cursor.fetchall()]
            if not ranked:
                return []
            hits = await self._load_hits([chunk_id for chunk_id, _ in ranked])
        return self._merge_hits(ranked, hits, mode="vector")

    async def search_keyword(self, *, owner_id: str, query: str, limit: int = 10) -> list[KnowledgeHit]:
        """按关键词检索。

        分两条路：查询串达到 3 字符走 FTS5（``trigram`` 的最小片段长度），更短的
        走 ``instr`` 子串匹配。

        WHY 回落用 ``instr`` 而不是 ``LIKE``：``LIKE`` 需要转义 ``%`` 与 ``_``，而那
        份转义规则已经在会话存储里存在一份，两处各写一遍迟早分叉——分叉的表现是
        「搜 50% 时返回了全部结果」。``instr`` 接受字面量，没有元字符，从根上不需要
        转义。

        Raises:
            ValueError: 参数非法。
        """
        owner = self._validate_owner(owner_id)
        capped = self._validate_limit(limit)
        normalized = self._validate_query(query)

        if len(normalized) >= _MIN_FTS_QUERY_CHARS:
            return await self._search_fts(owner, normalized, capped)
        return await self._search_instr(owner, normalized, capped)

    async def _search_fts(self, owner_id: str, query: str, limit: int) -> list[KnowledgeHit]:
        """FTS5 检索；查询串作为**短语**匹配。

        WHY 加引号：不加引号时 FTS5 会把查询串当表达式解析（空格是 AND、``-`` 是取反、
        ``*`` 是前缀），用户搜「登录 -超时」会得到一处语法错误或完全意外的结果。加引号
        后按字面短语匹配，与用户预期一致。短语内的双引号需要按 FTS5 规则翻倍转义。
        """
        phrase = '"' + query.replace('"', '""') + '"'
        async with self._lock:
            async with self._conn.execute(
                """
                SELECT c.chunk_id, c.doc_id, c.ordinal, c.heading, c.body,
                       d.source_path, bm25(knowledge_chunks_fts) AS score
                FROM knowledge_chunks_fts
                JOIN knowledge_chunks c ON c.chunk_id = knowledge_chunks_fts.rowid
                JOIN knowledge_documents d ON d.doc_id = c.doc_id
                WHERE knowledge_chunks_fts MATCH ? AND c.owner_id = ?
                ORDER BY score LIMIT ?
                """,
                (phrase, owner_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            KnowledgeHit(
                chunk_id=int(row["chunk_id"]),
                doc_id=int(row["doc_id"]),
                source_path=str(row["source_path"]),
                ordinal=int(row["ordinal"]),
                heading=str(row["heading"]),
                body=str(row["body"]),
                score=float(row["score"]),
                mode="keyword_fts",
            )
            for row in rows
        ]

    async def _search_instr(self, owner_id: str, query: str, limit: int) -> list[KnowledgeHit]:
        """短查询的子串回落（``instr``），按出现次数排序。

        WHY 用出现次数而不是首个位置：出现次数是一个粗糙但方向正确的相关性信号
        ——提到三次「超时」的块，比顺带提一次的更可能是在讲这件事。
        """
        async with self._lock:
            async with self._conn.execute(
                """
                SELECT c.chunk_id, c.doc_id, c.ordinal, c.heading, c.body,
                       d.source_path,
                       -((length(c.body) - length(replace(c.body, ?, ''))) / length(?)) AS score
                FROM knowledge_chunks c
                JOIN knowledge_documents d ON d.doc_id = c.doc_id
                WHERE c.owner_id = ? AND instr(c.body, ?) > 0
                ORDER BY score ASC, c.chunk_id ASC LIMIT ?
                """,
                (query, query, owner_id, query, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            KnowledgeHit(
                chunk_id=int(row["chunk_id"]),
                doc_id=int(row["doc_id"]),
                source_path=str(row["source_path"]),
                ordinal=int(row["ordinal"]),
                heading=str(row["heading"]),
                body=str(row["body"]),
                score=float(row["score"]),
                mode="keyword_like",
            )
            for row in rows
        ]

    async def _load_hits(self, chunk_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """按 chunk_id 批量取回正文与来源（调用方必须已持有锁）。

        Raises:
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" * len(chunk_ids))
        async with self._conn.execute(
            f"""
            SELECT c.chunk_id, c.doc_id, c.ordinal, c.heading, c.body, d.source_path
            FROM knowledge_chunks c
            JOIN knowledge_documents d ON d.doc_id = c.doc_id
            WHERE c.chunk_id IN ({placeholders})
            """,
            tuple(chunk_ids),
        ) as cursor:
            rows = await cursor.fetchall()
        return {int(row["chunk_id"]): dict(row) for row in rows}

    @staticmethod
    def _merge_hits(
        ranked: Sequence[tuple[int, float]], loaded: dict[int, dict[str, Any]], *, mode: str
    ) -> list[KnowledgeHit]:
        """按检索给出的名次合并正文，跳过已在索引中丢失的行。

        WHY 跳过而不是报错：向量表与正文表在同一事务里写入，理论上不会缺行；真要出现
        缺行说明库被外部改过，返回少几条命中远好过让整次检索失败。
        """
        hits: list[KnowledgeHit] = []
        for chunk_id, score in ranked:
            row = loaded.get(chunk_id)
            if row is None:
                logger.warning("向量索引命中的分块已不在正文表：chunk_id=%s", chunk_id)
                continue
            hits.append(
                KnowledgeHit(
                    chunk_id=chunk_id,
                    doc_id=int(row["doc_id"]),
                    source_path=str(row["source_path"]),
                    ordinal=int(row["ordinal"]),
                    heading=str(row["heading"]),
                    body=str(row["body"]),
                    score=score,
                    mode=mode,
                )
            )
        return hits

    async def stats(self, *, owner_id: str) -> dict[str, Any]:
        """返回某个主体的索引规模，供 ``GET /api/knowledge`` 使用。

        Raises:
            ValueError: ``owner_id`` 非法。
        """
        owner = self._validate_owner(owner_id)
        async with self._lock:
            async with self._conn.execute(
                "SELECT count(*), coalesce(sum(chunk_count), 0) FROM knowledge_documents WHERE owner_id = ?",
                (owner,),
            ) as cursor:
                row = await cursor.fetchone()
            document_count = int(row[0]) if row is not None else 0
            chunk_count = int(row[1]) if row is not None else 0
            vector_count = 0
            if self.vector_enabled:
                async with self._conn.execute(
                    """
                    SELECT count(*) FROM knowledge_chunks_vec v
                    WHERE v.owner_id = ?
                    """,
                    (owner,),
                ) as cursor:
                    vector_row = await cursor.fetchone()
                vector_count = int(vector_row[0]) if vector_row is not None else 0
        return {
            "owner_id": owner,
            "document_count": document_count,
            "chunk_count": chunk_count,
            "vector_count": vector_count,
            "vector_enabled": self.vector_enabled,
            "dims": self.dims,
            "model": self.model,
        }

    async def ping(self) -> bool:
        """探测数据库连通性。

        Raises:
            RuntimeError: 查询未返回结果行。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        async with self._lock:
            async with self._conn.execute("SELECT 1") as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("知识库连通性探测未返回结果行")
        return True


async def _read_meta(conn: aiosqlite.Connection) -> dict[str, str]:
    """读取库结构元信息。"""
    async with conn.execute("SELECT key, value FROM knowledge_meta") as cursor:
        rows = await cursor.fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


async def _write_meta(conn: aiosqlite.Connection, values: dict[str, str]) -> None:
    """写入（或覆盖）库结构元信息。"""
    for key, value in values.items():
        await conn.execute(
            "INSERT INTO knowledge_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


@asynccontextmanager
async def open_knowledge_store(
    db_path: Path,
    *,
    dims: int,
    model: str = "",
    vector_enabled: bool = True,
) -> AsyncIterator[KnowledgeStore]:
    """打开（并按需初始化）知识库。

    Args:
        db_path: SQLite 文件路径，父目录会自动创建。
        dims: 向量维度；``vector_enabled`` 为 True 时必须与已有索引一致。
        model: 嵌入模型标识；与维度一起用于识别「换了模型」。
        vector_enabled: 是否启用向量检索。

    Yields:
        已建表的 ``KnowledgeStore``。

    Raises:
        ValueError: 参数非法。
        KnowledgeStoreError: 存量索引的维度或模型与当前配置不符（必须重建索引），
            或指定启用向量检索但扩展无法加载。
        aiosqlite.Error: 建表或 PRAGMA 设置失败时原样向上抛出。

    Note:
        WHY 维度变化要**报错**而不是自动重建：重建会丢掉用户已索引的全部文档，而
        「换了模型」这件事只有用户知道该不该重来。报错把决定权交回去，同时给出
        明确的重建方式；静默重建则是把一次配置变更变成一次数据丢失。
    """
    if db_path is None:
        raise ValueError("db_path 不能为 None")
    if not isinstance(dims, int) or isinstance(dims, bool) or dims < 1:
        raise ValueError(f"dims 必须是正整数，实际：{dims!r}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn: aiosqlite.Connection | None = None
    loaded_extension = False
    try:
        # WHY 只把初始化包在捕获里、``yield`` 留在它外面：``yield`` 之后抛出的异常来自
        # ``async with`` 主体（调用方的装配或业务代码），这里接住会把它记成「知识库初始化
        # 失败」——主体里一个 ValueError 就能让每个存储各打一份「初始化失败 + 堆栈」，
        # 把排查引向数据库，而数据库根本没问题。连接失败同样是初始化失败，故一并包住。
        try:
            conn = await aiosqlite.connect(str(db_path))
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA busy_timeout=5000;")

            if vector_enabled:
                # WHY 用公开 API 而不是 ``conn._conn``：aiosqlite 本身暴露了
                # enable_load_extension / load_extension，摸私有字段只是把「上游实现
                # 细节」变成自己的依赖。``loadable_path()`` 给出的是无扩展名的路径，
                # 由 SQLite 自己补平台后缀（Windows .dll / POSIX .so）——实测可行。
                try:
                    import sqlite_vec

                    await conn.enable_load_extension(True)
                    await conn.load_extension(sqlite_vec.loadable_path())
                    loaded_extension = True
                except Exception as exc:
                    # WHY 直接报错而不是降级成关键词检索：启用向量检索是用户的显式选择，
                    # 而扩展加载失败是**启动期就能确定**的环境问题。静默降级会让知识库
                    # 看起来在工作、只是「搜不太准」——那种现象没人会去查扩展有没有加载。
                    raise KnowledgeStoreError(
                        f"EMBEDDING_BACKEND 要求向量检索，但 sqlite-vec 扩展加载失败："
                        f"{type(exc).__name__}: {exc}"
                    ) from exc

            await conn.executescript(_SCHEMA)
            if loaded_extension:
                await conn.executescript(_vec_table_ddl(dims))

            meta = await _read_meta(conn)
            recorded_version = meta.get("schema_version", "")
            if recorded_version and recorded_version != str(_SCHEMA_VERSION):
                raise KnowledgeStoreError(
                    f"知识库结构版本不符：库内 {recorded_version}，当前 {_SCHEMA_VERSION}；"
                    "请重建索引"
                )
            if loaded_extension:
                recorded_dims = meta.get("embedding_dims", "")
                recorded_model = meta.get("embedding_model", "")
                if recorded_dims and int(recorded_dims) != dims:
                    raise KnowledgeStoreError(
                        f"存量索引的向量维度是 {recorded_dims}，当前配置是 {dims}；"
                        "换过嵌入模型必须重建知识库（删除 .data/knowledge.db 后重新索引）"
                    )
                if recorded_model and recorded_model != model:
                    raise KnowledgeStoreError(
                        f"存量索引来自模型 {recorded_model}，当前配置是 {model}；"
                        "不同模型产出的向量无法互相比较，必须重建知识库"
                    )
                await _write_meta(
                    conn, {"embedding_dims": str(dims), "embedding_model": model}
                )
            await _write_meta(conn, {"schema_version": str(_SCHEMA_VERSION)})
            await conn.commit()
        except Exception:
            logger.exception("知识库初始化失败：%s", db_path)
            raise

        logger.info(
            "知识库已就绪：%s（dims=%s model=%s vectors=%s）",
            db_path,
            dims,
            model or "-",
            loaded_extension,
        )
        yield KnowledgeStore(
            conn, dims=dims, model=model, vector_enabled=loaded_extension
        )
    finally:
        if conn is not None:
            await conn.close()
            logger.debug("知识库连接已关闭：%s", db_path)
