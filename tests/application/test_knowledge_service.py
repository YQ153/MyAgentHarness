"""知识库服务：切分、索引、混合检索与降级。

重点覆盖三处**不会报错但会悄悄出错**的地方：

1. **中文切分**：分隔符顺序错了（空格排在「。」之前），一段没有空格的中文会被整段
   当作不可切分单元，产出远超块长的块——而超长块会被嵌入模型静默截断，表现是
   「文档后半段怎么都搜不到」。用一段长中文来钉住它。
2. **降级可观测**：嵌入失败时必须仍然索引成功（关键词可用），但返回值要带
   ``failed``。若把降级做成无声的，用户只会觉得「语义检索某天开始不准」。
3. **归属隔离**：检索只返回本主体的片段。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from application.errors import UnsupportedDocumentError
from application.knowledge_service import KnowledgeService, split_document
from config import AppConfig
from llm.embeddings import EmbeddingError
from runtime.knowledge_store import ChunkInput, open_knowledge_store
from runtime.workspace_files import WorkspacePathError
from tests.conftest import make_config, make_root

_DIMS = 4


class _FakeEmbeddings:
    """确定性的嵌入替身：按文本长度造一个可比较的向量。

    WHY 不用真模型：这一层要验的是**编排**（何时嵌入、失败怎么办、向量怎么进库），
    真模型会带来 189 MB 与一次冷下载，且失败原因不再可分辨。真模型链路由
    ``scripts/smoke_embed_backend.py`` 单独验证。
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.name = "fake:test"
        self.dims = _DIMS
        self.embed_calls = 0
        self._fail = fail

    async def embed(self, texts: Any) -> list[list[float]]:
        self.embed_calls += 1
        if self._fail:
            raise EmbeddingError("替身：嵌入服务不可用")
        return [[float(len(text) % 5), 1.0, 0.0, 0.0][:_DIMS] for text in texts]

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[tuple[KnowledgeService, AppConfig]]:
    """无嵌入后端的知识库服务（``EMBEDDING_BACKEND=none`` 的等价形态）。"""
    config = make_config(tmp_path)
    make_root(config).root.mkdir(parents=True, exist_ok=True)
    async with open_knowledge_store(
        tmp_path / "knowledge.db", dims=_DIMS, model="", vector_enabled=False
    ) as store:
        yield KnowledgeService(config, scope=make_root(config), store=store), config


@pytest.fixture
async def vector_service(tmp_path: Path) -> AsyncIterator[tuple[KnowledgeService, AppConfig, _FakeEmbeddings]]:
    """带嵌入后端的知识库服务（向量检索可用）。"""
    config = make_config(tmp_path)
    make_root(config).root.mkdir(parents=True, exist_ok=True)
    embeddings = _FakeEmbeddings()
    async with open_knowledge_store(
        tmp_path / "knowledge.db", dims=_DIMS, model="fake:test"
    ) as store:
        yield KnowledgeService(config, scope=make_root(config), store=store, embeddings=embeddings), config, embeddings


def _write(config: AppConfig, relative: str, text: str) -> str:
    """在工作区里写一份文件，返回其虚拟路径。"""
    target = make_root(config).root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return "/" + relative


# --------------------------------------------------------------- 切分


def test_split_attaches_heading_path() -> None:
    """每个块带上「它在哪一节下面」，检索结果因此能交代出处。"""
    chunks = split_document(
        "# 登录问题\n\n正文甲。\n\n## 超时\n\n正文乙。", chunk_chars=800, overlap_chars=100
    )

    headings = [chunk.heading for chunk in chunks]
    assert "登录问题" in headings
    assert "登录问题 > 超时" in headings


def test_split_long_chinese_text_is_broken_by_punctuation() -> None:
    """没有空格的长中文必须被切分。

    WHY 这条最关键：分隔符顺序错了（空格先于「。」）时，一段无空格的中文会被整段当成
    不可切分单元，产出一个远超块长的块，而它会被嵌入模型静默截断——用户只会发现
    「后半段搜不到」。
    """
    sentence = "这是一句用于验证中文切分边界的话，必须能被句末标点切开。"
    text = sentence * 40

    chunks = split_document(text, chunk_chars=200, overlap_chars=40)

    assert len(chunks) > 1
    assert all(len(chunk.body) <= 200 for chunk in chunks)
    # 切在句末标点之后：每个块都应以完整句子收尾
    assert all(chunk.body.endswith("。") for chunk in chunks)


def test_split_ordinals_are_contiguous() -> None:
    """``ordinal`` 必须从 0 连续——存储层对此有校验，服务层不能造出不合法输入。"""
    chunks = split_document("# 标题\n\n" + "内容。" * 200, chunk_chars=120, overlap_chars=20)

    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


def test_split_empty_document_returns_no_chunks() -> None:
    """空文档不产出分块（存储层拒绝空分块列表）。"""
    assert split_document("", chunk_chars=800, overlap_chars=100) == []
    assert split_document("   \n\n  ", chunk_chars=800, overlap_chars=100) == []


def test_split_overlap_keeps_boundary_sentence_intact() -> None:
    """重叠让边界句至少在某一侧保持完整。

    WHY：不重叠时被切断的句子在左右两块里各少一半，于是这句话在**两个块里都检索不到**。
    """
    text = "第一句话说的是甲。第二句话说的是乙。第三句话说的是丙。"
    chunks = split_document(text, chunk_chars=18, overlap_chars=12)

    assert len(chunks) > 1
    assert any("第二句话说的是乙。" in chunk.body for chunk in chunks)


# --------------------------------------------------------------- 索引


async def test_index_document_reports_count_and_status(
    service: tuple[KnowledgeService, AppConfig],
) -> None:
    """无嵌入后端时索引成功，且状态标明向量未启用。"""
    knowledge, config = service
    path = _write(config, "notes/login.md", "# 登录\n\n登录接口超时排查记录。")

    result = await knowledge.index_document(path)

    assert result["status"] == "indexed"
    assert result["chunk_count"] >= 1
    assert result["vector_status"] == "disabled"


async def test_indexed_document_is_searchable(
    service: tuple[KnowledgeService, AppConfig],
) -> None:
    """索引后能按关键词检索到，并带上出处与标题。"""
    knowledge, config = service
    path = _write(config, "notes/login.md", "# 登录问题\n\n登录接口超时排查记录。")
    await knowledge.index_document(path)

    result = await knowledge.search("登录接口")

    assert result["vector_status"] == "disabled"
    assert len(result["hits"]) == 1
    assert result["hits"][0]["source_path"] == path
    assert result["hits"][0]["heading"] == "登录问题"


async def test_reindexing_unchanged_document_is_skipped(
    service: tuple[KnowledgeService, AppConfig],
) -> None:
    """内容未变时跳过——每次重跑都重新嵌入会白白消耗模型调用。"""
    knowledge, config = service
    path = _write(config, "notes/login.md", "登录接口超时排查记录。")
    await knowledge.index_document(path)

    result = await knowledge.index_document(path)

    assert result["status"] == "unchanged"


async def test_force_reindexes_unchanged_document(
    service: tuple[KnowledgeService, AppConfig],
) -> None:
    """``force=True`` 时忽略指纹，强制重建。"""
    knowledge, config = service
    path = _write(config, "notes/login.md", "登录接口超时排查记录。")
    await knowledge.index_document(path)

    result = await knowledge.index_document(path, force=True)

    assert result["status"] == "indexed"


async def test_index_uses_vectors_when_embedding_available(
    vector_service: tuple[KnowledgeService, AppConfig, _FakeEmbeddings],
) -> None:
    """有嵌入后端时向量写进库，且索引阶段只嵌入一次。"""
    knowledge, config, embeddings = vector_service
    path = _write(config, "notes/login.md", "# 登录\n\n登录接口超时排查记录。")

    result = await knowledge.index_document(path)

    assert result["vector_status"] == "ok"
    assert embeddings.embed_calls == 1
    stats = (await knowledge.list_documents())["stats"]
    assert stats["vector_count"] == stats["chunk_count"] > 0


async def test_index_degrades_to_keyword_when_embedding_fails(tmp_path: Path) -> None:
    """嵌入失败时仍索引成功，但状态必须标明降级。

    WHY：文档已经在工作区里，能按关键词搜到远好过一条都搜不到；而状态带 ``failed``
    让这次降级可被看见，不会变成「语义检索某天开始不准」。
    """
    config = make_config(tmp_path)
    make_root(config).root.mkdir(parents=True, exist_ok=True)
    async with open_knowledge_store(tmp_path / "k.db", dims=_DIMS, model="fake:test") as store:
        knowledge = KnowledgeService(config, scope=make_root(config), store=store, embeddings=_FakeEmbeddings(fail=True))
        path = _write(config, "notes/login.md", "登录接口超时排查记录。")

        result = await knowledge.index_document(path)

        assert result["status"] == "indexed"
        assert result["vector_status"] == "failed"
        # 降级后关键词检索仍然可用
        assert (await knowledge.search("登录接口"))["hits"]


async def test_index_rejects_binary_file(service: tuple[KnowledgeService, AppConfig]) -> None:
    """二进制文件被显式拒绝，而不是索引进一堆乱码。"""
    knowledge, config = service
    target = make_root(config).root / "blob.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x01\x02binary")

    with pytest.raises(UnsupportedDocumentError, match="二进制"):
        await knowledge.index_document("/blob.bin")


async def test_index_rejects_non_utf8_file(service: tuple[KnowledgeService, AppConfig]) -> None:
    """非 UTF-8 文本给出可操作的失败信息，而不是靠宽松解码吞下乱码。"""
    knowledge, config = service
    target = make_root(config).root / "gbk.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes("中文内容".encode("gb18030"))

    with pytest.raises(UnsupportedDocumentError, match="UTF-8"):
        await knowledge.index_document("/gbk.txt")


async def test_index_rejects_oversized_file(tmp_path: Path) -> None:
    """超过字节上限的文件被拒绝，并说明实际大小。

    WHY 覆写上限而不是写一个巨大的文件：默认上限是 5 MB，真造一个 5 MB 文件只是为了让
    断言变慢；把上限压到 1 KB 能测到同一条分支。
    """
    config = make_config(tmp_path, workspace_file_max_bytes=1024)
    make_root(config).root.mkdir(parents=True, exist_ok=True)
    async with open_knowledge_store(
        tmp_path / "k.db", dims=_DIMS, model="", vector_enabled=False
    ) as store:
        knowledge = KnowledgeService(config, scope=make_root(config), store=store)
        target = make_root(config).root / "big.txt"
        target.write_text("字" * 500, encoding="utf-8")

        with pytest.raises(UnsupportedDocumentError, match="超过索引上限"):
            await knowledge.index_document("/big.txt")


async def test_index_missing_file_raises(service: tuple[KnowledgeService, AppConfig]) -> None:
    """文件不存在时报 FileNotFoundError（路由据此映射 404）。"""
    knowledge, _ = service

    with pytest.raises(FileNotFoundError):
        await knowledge.index_document("/nope.md")


async def test_index_rejects_path_outside_workspace(
    service: tuple[KnowledgeService, AppConfig],
) -> None:
    """逃出工作区的路径被拦下（与文件面板共用同一套拦截）。"""
    knowledge, _ = service

    with pytest.raises(WorkspacePathError):
        await knowledge.index_document("/../outside.md")


async def test_index_truncates_chunks_beyond_cap(tmp_path: Path) -> None:
    """分块数超限时截断并记日志，而不是拒绝让用户无法索引。"""
    config = make_config(tmp_path, knowledge_chunk_chars=100, knowledge_chunk_overlap_chars=10, knowledge_max_chunks_per_document=3)
    make_root(config).root.mkdir(parents=True, exist_ok=True)
    async with open_knowledge_store(
        tmp_path / "k.db", dims=_DIMS, model="", vector_enabled=False
    ) as store:
        knowledge = KnowledgeService(config, scope=make_root(config), store=store)
        path = _write(config, "big.md", "这一句用来填充内容，需要足够长才能切出多块。" * 40)

        result = await knowledge.index_document(path)

        assert result["chunk_count"] == 3


# --------------------------------------------------------------- 检索


async def test_search_is_isolated_by_owner(service: tuple[KnowledgeService, AppConfig]) -> None:
    """检索只返回本命名空间的片段。

    WHY 这里经存储层预置另一个 ``owner_id`` 的同名文档：服务层的归属固定为空串，
    没法**通过服务接口**造出「另一个归属」——而隔离本身正是在存储层按 ``owner_id``
    实现的，故从那一层注入数据来验证它确实生效。
    """
    knowledge, config = service
    path = _write(config, "notes/login.md", "登录接口超时排查记录。")
    await knowledge.index_document(path)
    await knowledge._store.replace_document(  # noqa: SLF001 - 见上方说明
        owner_id="someone-else",
        source_path="/other/login.md",
        content_hash="x",
        chunks=[ChunkInput(ordinal=0, heading="", body="登录接口超时排查记录。")],
        vectors=None,
    )

    hits = (await knowledge.search("登录接口"))["hits"]

    assert [hit["source_path"] for hit in hits] == [path]


async def test_search_rejects_empty_query(service: tuple[KnowledgeService, AppConfig]) -> None:
    """空检索词报错（路由据 ValueError 映射 400）。"""
    knowledge, _ = service

    with pytest.raises(ValueError, match="不能为空"):
        await knowledge.search("   ")


async def test_search_degrades_when_query_embedding_fails(tmp_path: Path) -> None:
    """查询嵌入失败时仍返回关键词结果，并标明降级。"""
    config = make_config(tmp_path)
    make_root(config).root.mkdir(parents=True, exist_ok=True)
    async with open_knowledge_store(tmp_path / "k.db", dims=_DIMS, model="fake:test") as store:
        knowledge = KnowledgeService(config, scope=make_root(config), store=store, embeddings=_FakeEmbeddings())
        path = _write(config, "notes/login.md", "登录接口超时排查记录。")
        await knowledge.index_document(path)
        # 让查询阶段的嵌入失败（索引阶段已成功）
        knowledge._embeddings = _FakeEmbeddings(fail=True)  # noqa: SLF001

        result = await knowledge.search("登录接口")

        assert result["vector_status"] == "failed"
        assert result["hits"], "降级后关键词结果仍应返回"
        assert result["modes"] == ["keyword_fts"]


async def test_search_merges_vector_and_keyword_hits(
    vector_service: tuple[KnowledgeService, AppConfig, _FakeEmbeddings],
) -> None:
    """两条路都命中同一分块时只返回一条（按名次融合去重）。"""
    knowledge, config, _ = vector_service
    path = _write(config, "notes/login.md", "登录接口超时排查记录。")
    await knowledge.index_document(path)

    result = await knowledge.search("登录接口")

    assert result["vector_status"] == "ok"
    assert len(result["hits"]) == 1


async def test_search_respects_limit(service: tuple[KnowledgeService, AppConfig]) -> None:
    """条数上限生效。"""
    knowledge, config = service
    for index in range(3):
        await knowledge.index_document(
            _write(config, f"notes/doc{index}.md", f"登录接口超时排查记录第 {index} 份。")
        )

    assert len((await knowledge.search("登录接口", limit=2))["hits"]) == 2


# --------------------------------------------------------------- 工作区与清单


async def test_index_workspace_skips_binary_and_hidden_dirs(
    service: tuple[KnowledgeService, AppConfig],
) -> None:
    """扫描时跳过二进制文件与点开头目录。

    WHY：``.attachments`` 与 ``.tool_outputs`` 是上传字节与工具输出留存，把它们的
    内容索引进知识库，检索结果里就会混进大量用户从没打算当作资料的东西。
    """
    knowledge, config = service
    _write(config, "notes/login.md", "登录接口超时排查记录。")
    (make_root(config).root / ".tool_outputs").mkdir(parents=True, exist_ok=True)
    (make_root(config).root / ".tool_outputs" / "out.txt").write_text("残留输出", encoding="utf-8")
    (make_root(config).root / "blob.bin").write_bytes(b"\x00\x01binary")

    summary = await knowledge.index_workspace()

    assert summary["indexed"] == 1
    assert summary["skipped"] == 1
    assert [item["source_path"] for item in summary["items"] if item["status"] == "indexed"] == [
        "/notes/login.md"
    ]


async def test_list_documents_reports_capabilities(
    service: tuple[KnowledgeService, AppConfig],
) -> None:
    """清单接口一并下发能力与统计（前端据此决定展示哪种检索状态）。"""
    knowledge, config = service
    await knowledge.index_document(_write(config, "notes/login.md", "登录接口超时排查记录。"))

    result = await knowledge.list_documents()

    assert [item["source_path"] for item in result["items"]] == ["/notes/login.md"]
    assert result["stats"]["document_count"] == 1
    assert result["capabilities"]["vector_enabled"] is False


async def test_remove_document_drops_it_from_search(
    service: tuple[KnowledgeService, AppConfig],
) -> None:
    """移除后检索不到；源文件不受影响。"""
    knowledge, config = service
    path = _write(config, "notes/login.md", "登录接口超时排查记录。")
    await knowledge.index_document(path)

    assert await knowledge.remove_document(path) is True
    assert (await knowledge.search("登录接口"))["hits"] == []
    assert (make_root(config).root / "notes/login.md").is_file()
