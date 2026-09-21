"""知识库存储层：索引、检索与跨主体隔离。

重点覆盖三类**不会报错但会悄悄出错**的地方：

1. **幂等与替换**：重复索引同一文件若变成新增，检索会返回重复片段；替换时若漏删
   旧块，就会留下检索得到、正文却早已过期的幽灵命中。
2. **短查询回落**：``trigram`` 对 2 字查询零命中（探针实测），必须走 ``instr``；
   这条若回归，表现是「某些词怎么都搜不到」，而不会让任何断言变红——所以单独测。
3. **跨主体隔离**：向量检索必须在 **SQL 层**按主体过滤。若改成先取 top-k 再在
   Python 里筛，别人的片段会先被读出来，且 k 很小时自己一条都剩不下。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from runtime.knowledge_store import (
    ChunkInput,
    KnowledgeStore,
    KnowledgeStoreError,
    open_knowledge_store,
)

_DIMS = 4
_MODEL = "test-model"

_VEC_X = [1.0, 0.0, 0.0, 0.0]
_VEC_NEAR_X = [0.9, 0.1, 0.0, 0.0]
_VEC_Y = [0.0, 1.0, 0.0, 0.0]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[KnowledgeStore]:
    """临时目录里的知识库（启用向量检索）。"""
    async with open_knowledge_store(
        tmp_path / "knowledge.db", dims=_DIMS, model=_MODEL
    ) as opened:
        yield opened


def _chunks(*bodies: str, heading: str = "") -> list[ChunkInput]:
    """按给定正文构造连续编号的分块。"""
    return [ChunkInput(ordinal=index, heading=heading, body=body) for index, body in enumerate(bodies)]


async def _index(
    store: KnowledgeStore,
    *,
    owner: str = "alice",
    path: str = "/notes/login.md",
    bodies: tuple[str, ...] = ("登录接口超时排查记录",),
    vectors: list[list[float]] | None = None,
    content_hash: str = "h1",
) -> dict:
    """把一个文档写进索引，返回写入后的文档行。"""
    return await store.replace_document(
        owner_id=owner,
        source_path=path,
        content_hash=content_hash,
        chunks=_chunks(*bodies),
        vectors=vectors if vectors is not None else [_VEC_X for _ in bodies],
    )


# --------------------------------------------------------------- 索引与幂等


async def test_indexed_document_is_readable(store: KnowledgeStore) -> None:
    """写入后文档行与统计口径一致。"""
    record = await _index(store, bodies=("登录接口超时排查记录", "数据库连接池配置"))

    assert record["chunk_count"] == 2
    assert record["source_path"] == "/notes/login.md"
    stats = await store.stats(owner_id="alice")
    assert stats["document_count"] == 1
    assert stats["chunk_count"] == 2
    assert stats["vector_count"] == 2


async def test_reindexing_same_document_does_not_duplicate(store: KnowledgeStore) -> None:
    """重复索引同一文件是一次替换，不是新增。

    WHY 关键：若变成新增，「重复索引」会让同一段正文在检索结果里出现多次，而用户
    只会觉得「检索结果里全是重复的」，很难联想到索引不是幂等的。
    """
    await _index(store)
    await _index(store, content_hash="h1")

    stats = await store.stats(owner_id="alice")
    assert stats["document_count"] == 1
    assert stats["chunk_count"] == 1

    hits = await store.search_keyword(owner_id="alice", query="登录接口")
    assert len(hits) == 1


async def test_reindexing_drops_chunks_that_no_longer_exist(store: KnowledgeStore) -> None:
    """替换后旧块必须消失——两个索引都要清干净。"""
    await _index(store, bodies=("登录接口超时排查记录", "数据库连接池配置"))
    await _index(store, bodies=("登录接口超时排查记录",))

    assert (await store.stats(owner_id="alice"))["chunk_count"] == 1
    # 关键词索引
    assert await store.search_keyword(owner_id="alice", query="连接池") == []
    # 向量索引（旧块的行必须一并删掉，否则向量检索仍会命中已不存在的正文）
    hits = await store.search_vector(owner_id="alice", vector=_VEC_X, limit=10)
    assert len(hits) == 1


async def test_delete_document_clears_both_indexes(store: KnowledgeStore) -> None:
    """删除后关键词与向量两条路都检索不到。"""
    await _index(store)

    assert await store.delete_document(owner_id="alice", source_path="notes/login.md") is True
    assert await store.get_document(owner_id="alice", source_path="notes/login.md") is None
    assert await store.search_keyword(owner_id="alice", query="登录接口") == []
    assert await store.search_vector(owner_id="alice", vector=_VEC_X, limit=10) == []


async def test_delete_missing_document_returns_false(store: KnowledgeStore) -> None:
    """删除不存在的文档返回 False，而不是报错。"""
    assert await store.delete_document(owner_id="alice", source_path="nope.md") is False


async def test_empty_chunks_are_rejected(store: KnowledgeStore) -> None:
    """空文档不应进入索引——否则会留下一个「零块的文档」占据清单。"""
    with pytest.raises(ValueError, match="chunks 不能为空"):
        await store.replace_document(
            owner_id="alice", source_path="empty.md", content_hash="h", chunks=[]
        )


async def test_non_contiguous_ordinals_are_rejected(store: KnowledgeStore) -> None:
    """``ordinal`` 必须从 0 连续——它是结果里交代「片段出处」的依据。"""
    with pytest.raises(ValueError, match="连续"):
        await store.replace_document(
            owner_id="alice",
            source_path="a.md",
            content_hash="h",
            chunks=[ChunkInput(ordinal=1, heading="", body="正文")],
            vectors=[_VEC_X],
        )


async def test_vector_count_must_match_chunks(store: KnowledgeStore) -> None:
    """向量条数与块数不符时必须报错，否则会把别人的向量配给自己的正文。"""
    with pytest.raises(ValueError, match="数量不符"):
        await store.replace_document(
            owner_id="alice",
            source_path="a.md",
            content_hash="h",
            chunks=_chunks("甲", "乙"),
            vectors=[_VEC_X],
        )


# --------------------------------------------------------------- 关键词检索


async def test_keyword_search_finds_chinese_fragment(store: KnowledgeStore) -> None:
    """三字及以上走 FTS5 trigram，并带上出处信息。"""
    await _index(store, bodies=("登录接口超时排查记录",))

    hits = await store.search_keyword(owner_id="alice", query="登录接口")

    assert len(hits) == 1
    assert hits[0].mode == "keyword_fts"
    assert hits[0].source_path == "/notes/login.md"
    assert hits[0].ordinal == 0


async def test_two_char_query_falls_back_to_instr(store: KnowledgeStore) -> None:
    """两字查询必须能搜到——回归「trigram 对短查询零命中」这条实测事实。

    WHY 单独测：``trigram`` 按三字符片段建索引，两字查询返回**零命中而不是报错**，
    因此若回落分支被误删，功能会静默失效。
    """
    await _index(store, bodies=("登录接口超时排查记录",))

    hits = await store.search_keyword(owner_id="alice", query="超时")

    assert len(hits) == 1
    assert hits[0].mode == "keyword_like"
    assert "超时" in hits[0].body


async def test_short_query_orders_by_occurrence_count(store: KnowledgeStore) -> None:
    """短查询按出现次数排序：提三次的比提一次的更相关。"""
    await _index(
        store,
        bodies=("超时，还是超时，又是超时", "顺带提一句超时"),
        vectors=[_VEC_X, _VEC_Y],
    )

    hits = await store.search_keyword(owner_id="alice", query="超时")

    assert [hit.ordinal for hit in hits] == [0, 1]


async def test_keyword_search_treats_query_as_phrase(store: KnowledgeStore) -> None:
    """查询串按字面短语匹配，不被当成 FTS 表达式。

    WHY：不加引号时 ``-`` 是取反、``*`` 是前缀、空格是 AND，用户搜「登录 -超时」
    会得到语法错误或完全意外的结果。
    """
    await _index(store, bodies=("登录接口一切正常", "另一个不相关的段落"))

    # 含 FTS 元字符的查询不应抛错；作为短语找不到任何东西也属正常
    hits = await store.search_keyword(owner_id="alice", query='登录 -超时')

    assert hits == []


# --------------------------------------------------------------- 向量检索


async def test_vector_search_orders_by_distance(store: KnowledgeStore) -> None:
    """离查询向量更近的排在前面。"""
    await _index(
        store,
        bodies=("甲的正文内容", "乙的正文内容"),
        vectors=[_VEC_Y, _VEC_NEAR_X],
    )

    hits = await store.search_vector(owner_id="alice", vector=_VEC_X, limit=10)

    assert [hit.ordinal for hit in hits] == [1, 0]
    assert hits[0].score < hits[1].score


async def test_vector_search_respects_limit(store: KnowledgeStore) -> None:
    """条数上限生效。"""
    await _index(store, bodies=("一", "二", "三"), vectors=[_VEC_X, _VEC_NEAR_X, _VEC_Y])

    assert len(await store.search_vector(owner_id="alice", vector=_VEC_X, limit=2)) == 2


async def test_vector_dimension_mismatch_is_rejected(store: KnowledgeStore) -> None:
    """查询向量维度不符时在入口报错，而不是交给扩展抛一条底层错误。"""
    with pytest.raises(ValueError, match="维度不符"):
        await store.search_vector(owner_id="alice", vector=[1.0, 0.0], limit=10)


# --------------------------------------------------------------- 跨主体隔离


async def test_keyword_search_is_isolated_by_owner(store: KnowledgeStore) -> None:
    """关键词检索只返回本主体的片段。"""
    await _index(store, owner="alice", path="/alice/login.md")
    await _index(store, owner="bob", path="/bob/login.md")

    hits = await store.search_keyword(owner_id="alice", query="登录接口")

    assert [hit.source_path for hit in hits] == ["/alice/login.md"]


async def test_vector_search_is_isolated_by_owner(store: KnowledgeStore) -> None:
    """向量检索的隔离发生在 SQL 层：别人的向量再近也不能出现。

    WHY 这条必须钉住：若把隔离挪到 Python 侧事后过滤，bob 的片段会先被读出来
    （在这里它离查询向量**更近**），而且当 alice 的命中不足 k 条时，结果条数会
    悄悄变少。
    """
    await _index(store, owner="bob", path="/bob/secret.md", vectors=[_VEC_X])
    await _index(store, owner="alice", path="/alice/login.md", vectors=[_VEC_Y])

    hits = await store.search_vector(owner_id="alice", vector=_VEC_X, limit=10)

    assert [hit.source_path for hit in hits] == ["/alice/login.md"]


async def test_delete_is_isolated_by_owner(store: KnowledgeStore) -> None:
    """一个主体不能删掉另一个主体的索引。"""
    await _index(store, owner="bob", path="bob/secret.md")

    assert await store.delete_document(owner_id="alice", source_path="bob/secret.md") is False
    assert await store.get_document(owner_id="bob", source_path="bob/secret.md") is not None


async def test_stats_are_isolated_by_owner(store: KnowledgeStore) -> None:
    """统计口径按主体分开。"""
    await _index(store, owner="alice", path="alice/a.md")
    await _index(store, owner="bob", path="bob/b.md", bodies=("一", "二"))

    alice = await store.stats(owner_id="alice")
    bob = await store.stats(owner_id="bob")

    assert (alice["document_count"], alice["chunk_count"]) == (1, 1)
    assert (bob["document_count"], bob["chunk_count"]) == (1, 2)
    assert bob["vector_count"] == 2


async def test_list_documents_is_isolated_by_owner(store: KnowledgeStore) -> None:
    """文档清单同样按主体过滤。"""
    await _index(store, owner="alice", path="alice/a.md")
    await _index(store, owner="bob", path="bob/b.md")

    assert [item["source_path"] for item in await store.list_documents(owner_id="alice")] == [
        "/alice/a.md"
    ]


# --------------------------------------------------------------- 结构与配置


async def test_dims_mismatch_on_existing_store_raises(tmp_path: Path) -> None:
    """存量索引的维度与当前配置不符时必须报错（换过模型要重建）。"""
    async with open_knowledge_store(tmp_path / "knowledge.db", dims=4, model=_MODEL):
        pass

    with pytest.raises(KnowledgeStoreError, match="维度"):
        async with open_knowledge_store(tmp_path / "knowledge.db", dims=8, model=_MODEL):
            pass


async def test_model_mismatch_on_existing_store_raises(tmp_path: Path) -> None:
    """维度相同但模型不同也要拦下：不同模型产出的向量无法互相比较。"""
    async with open_knowledge_store(tmp_path / "knowledge.db", dims=4, model="model-a"):
        pass

    with pytest.raises(KnowledgeStoreError, match="模型"):
        async with open_knowledge_store(tmp_path / "knowledge.db", dims=4, model="model-b"):
            pass


async def test_vector_disabled_keeps_keyword_search_working(tmp_path: Path) -> None:
    """不启用向量检索时，关键词检索照常工作（``none`` 档位的降级路径）。"""
    async with open_knowledge_store(
        tmp_path / "knowledge.db", dims=_DIMS, model="", vector_enabled=False
    ) as plain:
        await plain.replace_document(
            owner_id="alice",
            source_path="a.md",
            content_hash="h",
            chunks=_chunks("登录接口超时排查记录"),
        )

        assert plain.vector_enabled is False
        assert len(await plain.search_keyword(owner_id="alice", query="登录接口")) == 1
        with pytest.raises(KnowledgeStoreError, match="未启用向量检索"):
            await plain.search_vector(owner_id="alice", vector=_VEC_X, limit=10)


async def test_vectors_rejected_when_vector_disabled(tmp_path: Path) -> None:
    """未启用向量却传向量，必须在入口报错而不是悄悄丢弃。"""
    async with open_knowledge_store(
        tmp_path / "knowledge.db", dims=_DIMS, model="", vector_enabled=False
    ) as plain:
        with pytest.raises(KnowledgeStoreError, match="未启用向量检索"):
            await plain.replace_document(
                owner_id="alice",
                source_path="a.md",
                content_hash="h",
                chunks=_chunks("正文"),
                vectors=[_VEC_X],
            )


# --------------------------------------------------------------- 校验


async def test_source_path_is_normalized_to_posix(store: KnowledgeStore) -> None:
    """Windows 分隔符与重复分隔符都归一到 POSIX。

    WHY：索引时写 POSIX 路径，若允许原生分隔符混入，同一份文件会因写法不同被索引
    两次，而表现是「旧的索引删不掉」。
    """
    await _index(store, path="notes\\login.md")

    assert (await store.get_document(owner_id="alice", source_path="notes/login.md")) is not None
    assert (await store.get_document(owner_id="alice", source_path="/notes//login.md")) is not None


async def test_source_path_rejects_parent_traversal(store: KnowledgeStore) -> None:
    """含 ``..`` 的路径直接拒绝（索引来源必须限定在工作区内）。"""
    with pytest.raises(ValueError, match="上跳"):
        await _index(store, path="../outside.md")


async def test_query_validation(store: KnowledgeStore) -> None:
    """空查询与超长查询都报错。"""
    with pytest.raises(ValueError, match="query 不能为空"):
        await store.search_keyword(owner_id="alice", query="   ")
    with pytest.raises(ValueError, match="过长"):
        await store.search_keyword(owner_id="alice", query="字" * 500)


async def test_limit_validation(store: KnowledgeStore) -> None:
    """条数上限必须是 1..200 的整数。"""
    with pytest.raises(ValueError, match="limit"):
        await store.search_keyword(owner_id="alice", query="登录接口", limit=0)
    with pytest.raises(ValueError, match="limit"):
        await store.search_keyword(owner_id="alice", query="登录接口", limit=500)


async def test_ping_reports_connection_usable(store: KnowledgeStore) -> None:
    """连通性探测可用。"""
    assert await store.ping() is True
