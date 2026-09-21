"""知识库服务：把工作区文档切分、嵌入并索引，以及按语义与关键词检索。

职责边界（三条不变量）：

1. **切分与嵌入在本层，存储不在**——存储层按分层约定不能依赖 ``llm``，因此向量在
   本层算好再传进去；本层也不直接写 SQL。
2. **路径校验不在本层**——它只有一份实现（``runtime.workspace_files``），与文件面板、
   附件共用同一套拦截；在服务层再写一遍就等于给「目录穿越」留下第二个答案。
3. **嵌入不可用不等于检索不可用**——没有嵌入后端时检索走关键词；嵌入**调用失败**
   时降级为关键词并记 WARNING，同时在返回值里带 ``vector_status``。降级可以被看见，
   才不会变成「语义检索某天开始不准，但没人知道从哪天起」。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from application.errors import UnsupportedDocumentError
from application.ports import KnowledgeIndex
from llm.embeddings import EmbeddingError
from runtime.knowledge_store import ChunkInput, KnowledgeHit
from runtime.workspace_files import (
    WorkspacePathError,
    looks_binary,
    read_bytes_capped,
    resolve_in_workspace,
)
from text_utils import collapse_whitespace

if TYPE_CHECKING:
    from collections.abc import Sequence

    from config import AppConfig, SessionRoot
    from llm.embeddings import EmbeddingBackend

logger = logging.getLogger(__name__)

_OWNER_ID = ""
"""知识库记录的统一 ``owner_id``。

WHY 仍然带这一列：存储层按 ``owner_id`` 建索引与去重，把它从表结构里删掉是一次
破坏性的迁移；而本应用不区分用户，所有索引都属于同一个人，因此固定为空串——
与 ``thread_store`` 里「会话的 ``owner_id`` 为空串」保持同一表示。
"""

_MARKDOWN_HEADERS: list[tuple[str, str]] = [("#", "h1"), ("##", "h2"), ("###", "h3")]
"""识别为标题的 Markdown 层级；只到三级——更深的层级在检索结果里做出处标注反而啰嗦。"""

_CHUNK_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", " "]
"""切分优先级（从粗到细）。

WHY 中文标点必须排在空格之前：切分器按列表顺序尝试分隔符，若让空格先于「。」出现，
一段没有空格的中文会被整段当成一个不可切分单元，产出远超块长的块——而超长块会被
嵌入模型**静默截断**，表现为「文档后半段怎么都搜不到」。
"""

_SKIP_DIR_NAMES = frozenset({"node_modules", "__pycache__", ".git", ".venv", "venv"})
"""扫描工作区时整棵跳过的目录。

WHY：这些目录动辄上万文件，且内容与「用户放进来的资料」无关；不跳过会让一次
``index_workspace`` 变成一次全盘扫描。点开头的目录另由前缀判断统一跳过
（``.attachments`` / ``.tool_outputs`` 分别是上传字节与工具输出留存）。
"""

_RRF_K = 60
"""倒数排名融合的平滑常数。

取文献里的常用值：它的作用是压低头部名次的绝对优势，使「两路都排第二」的结果不至于
输给「一路排第一、另一路完全没出现」。具体数值不敏感，重要的是融合方式本身。
"""


def split_document(text: str, *, chunk_chars: int, overlap_chars: int) -> list[ChunkInput]:
    """把一份文档切成带标题归属的连续分块。

    WHY 先按标题分段再切长度：标题是文档作者给出的**语义边界**，比任何长度启发式都
    准；先分段还能让每个块带上「它在哪一节下面」，检索结果因此能交代出处，而不只是
    丢给模型一段孤立文字。

    Args:
        text: 文档正文。
        chunk_chars: 单个分块的目标字符数。
        overlap_chars: 相邻分块的重叠字符数。

    Returns:
        连续编号（``ordinal`` 从 0 开始）的分块；空文档返回空列表。
    """
    sections = _split_by_heading(text)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_chars,
        chunk_overlap=overlap_chars,
        separators=_CHUNK_SEPARATORS,
        # WHY 用 ``"end"`` 而不是 ``True``：后者会把分隔符挂到**下一块的开头**，于是每块都以
        # 一个孤立的「。」起头——那个字符既是给模型看的噪音，也参与嵌入计算；而结尾
        # 对齐让每块恰好收在句末，正文是完整的。
        keep_separator="end",
    )

    chunks: list[ChunkInput] = []
    for heading, body in sections:
        for piece in splitter.split_text(body):
            normalized = piece.strip()
            if not normalized:
                continue
            chunks.append(
                ChunkInput(ordinal=len(chunks), heading=heading, body=normalized)
            )
    return chunks


def _split_by_heading(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题把正文分段，并为每段算出「标题路径」。

    Returns:
        ``(标题路径, 段落正文)`` 列表；没有任何标题时返回单段（路径为空串）。
    """
    if not text.strip():
        return []
    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=_MARKDOWN_HEADERS, strip_headers=False
    )
    documents = splitter.split_text(text)
    if not documents:
        # 无标题的纯文本：整篇作为一段，标题路径留空
        return [("", text)]

    sections: list[tuple[str, str]] = []
    for document in documents:
        parts = [str(document.metadata.get(key, "")) for _, key in _MARKDOWN_HEADERS]
        sections.append((" > ".join(part for part in parts if part), document.page_content))
    return sections


def _merge_hits(
    ranked_lists: Sequence[Sequence[KnowledgeHit]], *, limit: int
) -> list[KnowledgeHit]:
    """按倒数排名融合多路检索结果。

    WHY 不按 score 直接合并：两路的 score 量纲不同（向量是 L2 距离、关键词是 bm25），
    直接比大小等于拿米和摄氏度做比较。按名次融合只需要「谁排第几」，与量纲无关。

    Args:
        ranked_lists: 各路已按相关性排好序的命中。
        limit: 融合后返回的条数上限。

    Returns:
        融合后的命中；同一分块在多路出现时只保留一条，``score`` 为融合分。
    """
    fused: dict[int, float] = {}
    first_seen: dict[int, KnowledgeHit] = {}

    for hits in ranked_lists:
        for rank, hit in enumerate(hits):
            fused[hit.chunk_id] = fused.get(hit.chunk_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
            first_seen.setdefault(hit.chunk_id, hit)

    ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
    return [replace(first_seen[chunk_id], score=score) for chunk_id, score in ordered[:limit]]


class KnowledgeService:
    """工作区文档的索引与检索。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        scope: SessionRoot,
        store: KnowledgeIndex,
        embeddings: EmbeddingBackend | None = None,
    ) -> None:
        """构造服务。

        Args:
            config: 应用配置，提供切分参数、检索条数上限与字节上限。
            scope: 本实例服务的工作区；**必填**。它同时是「索引哪片文档」与「这份索引
                属于谁」的判据——知识库按工作区各存一份，两个工作区里同名的
                ``/src/index.ts`` 不是同一份文档。
            store: 该工作区对应的知识库存储。
            embeddings: 嵌入后端；``None`` 表示只做关键词检索。

        Raises:
            ValueError: 任一必需依赖为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if scope is None:
            raise ValueError("scope 不能为 None：索引的扫描根由它决定")
        if store is None:
            raise ValueError("store 不能为 None")

        self._config = config
        self._store = store
        self._embeddings = embeddings
        self._root = scope.root
        logger.info(
            "KnowledgeService 就绪：workspace=%s 向量=%s 嵌入=%s",
            self._root,
            store.vector_enabled,
            embeddings.name if embeddings is not None else "none",
        )

    # ------------------------------------------------------------------ 能力

    def capabilities(self) -> dict[str, Any]:
        """当前生效的能力与参数，供接口与工具描述使用。"""
        return {
            "vector_enabled": self._store.vector_enabled,
            "embedding_backend": self._embeddings.name if self._embeddings is not None else None,
            "dims": self._store.dims,
            "model": self._store.model,
            "chunk_chars": self._config.knowledge_chunk_chars,
            "chunk_overlap_chars": self._config.knowledge_chunk_overlap_chars,
            "top_k": self._config.knowledge_search_top_k,
        }

    # ------------------------------------------------------------------ 索引

    async def index_document(
        self, source_path: str, *, force: bool = False
    ) -> dict[str, Any]:
        """索引（或重新索引）工作区内的一份文档。

        Args:
            source_path: 工作区虚拟路径，如 ``/notes/login.md``。
            force: 为 ``True`` 时忽略内容指纹，强制重建索引。

        Returns:
            本次索引的结果：``status`` 为 ``indexed`` / ``unchanged`` / ``empty``，
            另含 ``chunk_count`` 与 ``vector_status``。

        Raises:
            WorkspacePathError: 路径非法或逃出工作区。
            FileNotFoundError: 文件不存在。
            UnsupportedDocumentError: 二进制内容、非 UTF-8 或超过字节上限。
            ValueError: ``source_path`` 非法。
        """
        owner = _OWNER_ID
        virtual = _normalize_virtual(source_path)
        absolute = resolve_in_workspace(self._root, virtual)
        if not absolute.is_file():
            raise FileNotFoundError(f"文件不存在：{virtual}")

        text = await asyncio.to_thread(self._read_text, absolute, virtual)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        existing = await self._store.get_document(owner_id=owner, source_path=virtual)
        if existing is not None and existing.get("content_hash") == digest and not force:
            # WHY 用内容指纹而不是修改时间：时间戳在检出、复制、同步之后都会变，而内容
            # 没变；按时间判会做大量无意义的重复嵌入，而每次嵌入都要调用模型。
            logger.debug("文档内容未变，跳过索引：%s", virtual)
            return {
                "source_path": virtual,
                "status": "unchanged",
                "chunk_count": int(existing.get("chunk_count") or 0),
                "vector_status": "unchanged",
            }

        chunks = split_document(
            text,
            chunk_chars=self._config.knowledge_chunk_chars,
            overlap_chars=self._config.knowledge_chunk_overlap_chars,
        )
        if not chunks:
            logger.info("文档无可索引内容，跳过：%s", virtual)
            return {"source_path": virtual, "status": "empty", "chunk_count": 0, "vector_status": "none"}

        cap = self._config.knowledge_max_chunks_per_document
        if len(chunks) > cap:
            # WHY 截断而不是拒绝：超限的多是日志与生成文件，它们的前半部分往往正是用户
            # 想搜的内容；直接拒绝会让「这份文档索引不了」变成一个需要解释的失败。
            logger.warning("文档分块数超限，已截断：%s（%d > %d）", virtual, len(chunks), cap)
            chunks = chunks[:cap]

        vectors, vector_status = await self._embed_chunks(chunks)
        record = await self._store.replace_document(
            owner_id=owner,
            source_path=virtual,
            content_hash=digest,
            chunks=chunks,
            vectors=vectors,
        )
        logger.info(
            "文档已索引：owner=%s path=%s chunks=%d vectors=%s",
            owner or "-",
            virtual,
            len(chunks),
            vector_status,
        )
        return {
            "source_path": virtual,
            "status": "indexed",
            "chunk_count": int(record.get("chunk_count") or len(chunks)),
            "vector_status": vector_status,
        }

    async def index_workspace(self, *, force: bool = False) -> dict[str, Any]:
        """扫描工作区并索引其中全部文本文档。

        WHY 逐个文件吞掉可预期的失败：一份二进制或非 UTF-8 的文件不该让整次索引中断
        ——那会让「工作区里有一个怪文件」变成「知识库完全用不了」。被跳过的文件记
        WARNING 并逐条列在返回结果里，用户可以看见到底跳了什么。

        Returns:
            汇总结果：``indexed`` / ``unchanged`` / ``skipped`` 计数与逐条明细。
        """
        candidates = await asyncio.to_thread(self._discover)
        results: list[dict[str, Any]] = []

        for virtual in candidates:
            try:
                results.append(await self.index_document(virtual, force=force))
            except (UnsupportedDocumentError, WorkspacePathError, OSError) as exc:
                logger.warning("跳过无法索引的文件：%s（%s）", virtual, exc)
                results.append({"source_path": virtual, "status": "skipped", "detail": str(exc)})

        summary: dict[str, Any] = {"total": len(results), "scanned": len(candidates)}
        for status in ("indexed", "unchanged", "empty", "skipped"):
            summary[status] = sum(1 for item in results if item.get("status") == status)
        summary["items"] = results
        logger.info(
            "工作区索引完成：扫描 %d，新索引 %d，未变化 %d，跳过 %d",
            summary["scanned"],
            summary["indexed"],
            summary["unchanged"],
            summary["skipped"],
        )
        return summary

    def _discover(self) -> list[str]:
        """列出工作区里可索引的候选文件（虚拟路径）。

        WHY 跳过点开头目录与依赖目录：``.attachments`` / ``.tool_outputs`` 分别是上传
        字节与工具输出留存，``node_modules`` 之类是依赖——把它们索引进知识库，检索
        结果里就会混进大量用户从没打算当作「资料」的内容。
        """
        found: list[str] = []
        for path in sorted(self._root.rglob("*")):
            try:
                if not path.is_file():
                    continue
                relative = path.relative_to(self._root)
            except OSError:
                # 断链软链或权限不足：跳过而不是让整次扫描失败
                continue
            parts = relative.parts
            if any(part.startswith(".") or part in _SKIP_DIR_NAMES for part in parts[:-1]):
                continue
            if parts and (parts[-1].startswith(".") or parts[-1] in _SKIP_DIR_NAMES):
                continue
            found.append("/" + relative.as_posix())
        return found

    def _read_text(self, absolute: Path, virtual: str) -> str:
        """读取并解码文档正文。

        Raises:
            UnsupportedDocumentError: 超过字节上限、内容像二进制，或不是 UTF-8。
        """
        max_bytes = self._config.workspace_file_max_bytes
        data, total, truncated = read_bytes_capped(absolute, max_bytes=max_bytes)
        if truncated:
            raise UnsupportedDocumentError(
                virtual, f"文件 {total} 字节，超过索引上限 {max_bytes} 字节"
            )
        if looks_binary(data):
            raise UnsupportedDocumentError(virtual, "内容看起来是二进制（含空字节）")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            # WHY 只认 UTF-8、不做编码嗅探：宽松的回落（例如 gb18030）几乎能解开任意
            # 字节序列，于是「非文本文件」会被静默解成乱码并被索引进库——那正是最难
            # 发现的一类脏索引。宁可给出一条可操作的失败信息。
            raise UnsupportedDocumentError(
                virtual, f"不是 UTF-8 文本（{exc.reason}），请先转换编码"
            ) from exc

    async def _embed_chunks(
        self, chunks: Sequence[ChunkInput]
    ) -> tuple[list[list[float]] | None, str]:
        """嵌入全部分块。

        Returns:
            ``(vectors, status)``；``status`` 为 ``ok`` / ``disabled`` / ``failed``；
            未成功时 ``vectors`` 为 ``None``。

        WHY 失败时降级而不是让整次索引失败：文档已经在工作区里，能按关键词搜到远好过
        一条都搜不到。但降级必须可观测——记 WARNING 并把状态带回上层。
        """
        if self._embeddings is None or not self._store.vector_enabled:
            return None, "disabled"

        texts = [chunk.body for chunk in chunks]
        try:
            vectors = await self._embeddings.embed(texts)
        except EmbeddingError as exc:
            logger.warning("嵌入失败，本次索引降级为关键词检索：%s", exc)
            return None, "failed"
        if len(vectors) != len(texts):
            logger.warning(
                "嵌入返回条数不符（%d != %d），本次索引降级为关键词检索",
                len(vectors),
                len(texts),
            )
            return None, "failed"
        return vectors, "ok"

    # ------------------------------------------------------------------ 删除与列举

    async def remove_document(self, source_path: str) -> bool:
        """从知识库移除一份文档（不影响工作区里的源文件）。

        Returns:
            ``True`` 表示确实移除了一份已索引文档；``False`` 表示本来就没索引过。

        Raises:
            WorkspacePathError: 路径非法。
        """
        owner = _OWNER_ID
        virtual = _normalize_virtual(source_path)
        return await self._store.delete_document(owner_id=owner, source_path=virtual)

    async def list_documents(self) -> dict[str, Any]:
        """列出已索引的文档与规模。"""
        owner = _OWNER_ID
        documents = await self._store.list_documents(owner_id=owner)
        return {
            "owner_id": owner,
            "items": [
                {
                    "source_path": item["source_path"],
                    "chunk_count": int(item["chunk_count"]),
                    "indexed_at": item["indexed_at"],
                }
                for item in documents
            ],
            "stats": await self._store.stats(owner_id=owner),
            "capabilities": self.capabilities(),
        }

    # ------------------------------------------------------------------ 检索

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """检索知识库，返回融合后的片段。

        Args:
            query: 检索词。
            limit: 返回条数；``None`` 表示取配置的 ``KNOWLEDGE_SEARCH_TOP_K``。

        Returns:
            含 ``hits`` / ``vector_status`` / ``modes`` 的结果字典。

        Raises:
            ValueError: ``query`` 为空或超长、``limit`` 越界。
        """
        owner = _OWNER_ID
        top_k = self._config.knowledge_search_top_k if limit is None else limit
        normalized = collapse_whitespace(query)
        if not normalized:
            raise ValueError("检索词不能为空")

        keyword_hits = await self._store.search_keyword(
            owner_id=owner, query=normalized, limit=top_k
        )
        vector_hits, vector_status = await self._vector_search(
            owner=owner, query=normalized, limit=top_k
        )

        hits = _merge_hits([vector_hits, keyword_hits], limit=top_k)
        logger.debug(
            "知识库检索：query=%r vector=%d keyword=%d fused=%d（vector_status=%s）",
            normalized,
            len(vector_hits),
            len(keyword_hits),
            len(hits),
            vector_status,
        )
        return {
            "query": normalized,
            "hits": [
                {
                    "source_path": hit.source_path,
                    "ordinal": hit.ordinal,
                    "heading": hit.heading,
                    "body": hit.body,
                    "score": round(hit.score, 6),
                    "mode": hit.mode,
                }
                for hit in hits
            ],
            "vector_status": vector_status,
            "modes": sorted({hit.mode for hit in hits}),
        }

    async def _vector_search(
        self, *, owner: str, query: str, limit: int
    ) -> tuple[list[KnowledgeHit], str]:
        """向量检索；不可用时返回空列表与原因。

        Returns:
            ``(hits, status)``；``status`` 为 ``ok`` / ``disabled`` / ``failed``。
        """
        if self._embeddings is None or not self._store.vector_enabled:
            return [], "disabled"
        try:
            vectors = await self._embeddings.embed([query])
        except EmbeddingError as exc:
            logger.warning("查询嵌入失败，本次检索只用关键词：%s", exc)
            return [], "failed"
        if len(vectors) != 1:
            logger.warning("查询嵌入返回 %d 条（期望 1），本次检索只用关键词", len(vectors))
            return [], "failed"
        hits = await self._store.search_vector(owner_id=owner, vector=vectors[0], limit=limit)
        return hits, "ok"


def _normalize_virtual(source_path: str) -> str:
    """把调用方给的路径归一成工作区虚拟路径。

    WHY 在这里补一层而不只依赖 ``resolve_in_workspace``：该函数接受 ``/a.md`` 也能
    处理 ``a.md``，但知识库的存储键要求**写法唯一**——同一份文件以 ``a.md`` 与
    ``/a.md`` 各索引一次，就成了两条互不覆盖的记录，而表现是「旧索引删不掉」。

    Raises:
        ValueError: ``source_path`` 非字符串或为空。
    """
    if not isinstance(source_path, str):
        raise ValueError(f"source_path 必须是字符串，实际：{type(source_path).__name__}")
    candidate = collapse_whitespace(source_path).replace("\\", "/")
    if not candidate:
        raise ValueError("source_path 不能为空")
    return candidate if candidate.startswith("/") else "/" + candidate
