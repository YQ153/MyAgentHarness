"""知识库增量同步：新增 / 变更 / 删除都要落到索引上，而**未变的不能重做**。

最后一类是本文件的重点：「自动同步」如果每轮都重新嵌入一遍全部文档，它会退化成
一个持续占用嵌入后端与磁盘的负担——而那种退化**不会报错**，只会表现为「机器变卡、
嵌入服务变慢」。因此这里用「嵌入调用次数」把它钉住。

与 ``scripts/smoke_knowledge.py`` 的分工：那边用真模型验端到端，这里用替身嵌入验
编排（何时该重做、何时该跳过、删除有没有被跟进）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from application.knowledge_service import KnowledgeService
from config import AppConfig
from llm.embeddings import EmbeddingError
from runtime.knowledge_store import open_knowledge_store
from tests.conftest import make_config, make_root

_DIMS = 4


class _FakeEmbeddings:
    """记录调用次数的嵌入替身。

    WHY 要计数：增量与否的判据就是「有没有再调一次模型」，而不是「返回值对不对」。
    """

    def __init__(self) -> None:
        self.name = "fake:sync"
        self.dims = _DIMS
        self.embed_calls = 0

    async def embed(self, texts: Any) -> list[list[float]]:
        self.embed_calls += 1
        return [[float(len(text) % 5), 1.0, 0.0, 0.0][:_DIMS] for text in texts]

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[KnowledgeService, AppConfig, _FakeEmbeddings]]:
    """带嵌入替身的知识库服务。"""
    config = make_config(tmp_path)
    make_root(config).root.mkdir(parents=True, exist_ok=True)
    embeddings = _FakeEmbeddings()
    async with open_knowledge_store(
        tmp_path / "knowledge.db", dims=_DIMS, model="fake:sync"
    ) as store:
        yield KnowledgeService(
            config, scope=make_root(config), store=store, embeddings=embeddings
        ), config, embeddings


def _root(config: AppConfig) -> Path:
    return make_root(config).root


def _write(config: AppConfig, relative: str, text: str) -> Path:
    target = _root(config) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------- 新增与幂等


async def test_sync_indexes_new_document(env: tuple[KnowledgeService, AppConfig, _FakeEmbeddings]) -> None:
    """工作区里新出现的文档要被索引进库。"""
    service, config, _embeddings = env
    _write(config, "notes/login.md", "# 登录\n\n登录接口偶发超时，先看连接池。")

    summary = await service.sync_workspace()

    assert summary["indexed"] == 1, summary
    assert summary["scanned"] == 1, summary
    documents = await service.list_documents()
    assert [item["source_path"] for item in documents["items"]] == ["/notes/login.md"]


async def test_sync_second_run_does_not_reembed(env: tuple[KnowledgeService, AppConfig, _FakeEmbeddings]) -> None:
    """内容没变时，第二轮不得再调用嵌入后端。

    WHY 这条是核心：文件签名未变的文件必须被直接跳过（连内容都不读），否则自动同步
    就变成了一台每 5 分钟全量重算一遍的机器。
    """
    service, config, embeddings = env
    _write(config, "notes/login.md", "# 登录\n\n登录接口偶发超时，先看连接池。")

    await service.sync_workspace()
    calls_after_first = embeddings.embed_calls
    assert calls_after_first >= 1

    summary = await service.sync_workspace()

    assert embeddings.embed_calls == calls_after_first, "内容未变却重新调用了嵌入"
    assert summary["indexed"] == 0, summary
    assert summary["unchanged"] == 0, summary  # 未变的直接跳过，不进明细


# --------------------------------------------------------------- 变更


async def test_sync_reindexes_changed_document(env: tuple[KnowledgeService, AppConfig, _FakeEmbeddings]) -> None:
    """内容变了的文档要被重新索引，且检索到的是新内容。"""
    service, config, embeddings = env
    target = _write(config, "notes/login.md", "# 登录\n\n旧内容：这里讲的是会话保持。")

    await service.sync_workspace()
    calls_after_first = embeddings.embed_calls

    # 长度明显不同，确保 (mtime, size) 签名变化
    target.write_text("# 登录\n\n全新内容：这里讲的是幂等键由客户端生成，服务端只做校验，不负责去重。", encoding="utf-8")

    summary = await service.sync_workspace()

    assert summary["indexed"] == 1, summary
    assert embeddings.embed_calls > calls_after_first
    hits = await service.search("幂等键")
    assert [hit["source_path"] for hit in hits["hits"]] == ["/notes/login.md"]


# --------------------------------------------------------------- 删除


async def test_sync_removes_deleted_document(env: tuple[KnowledgeService, AppConfig, _FakeEmbeddings]) -> None:
    """文件从工作区删掉后，索引必须跟着消失（否则会检索到幽灵内容）。"""
    service, config, _embeddings = env
    target = _write(config, "notes/login.md", "# 登录\n\n登录接口偶发超时。")
    await service.sync_workspace()

    target.unlink()
    summary = await service.sync_workspace()

    assert summary["removed"] == 1, summary
    documents = await service.list_documents()
    assert documents["items"] == []
    assert (await service.search("登录"))["hits"] == []


# --------------------------------------------------------------- 不便索引的文件


async def test_sync_skips_binary_document(env: tuple[KnowledgeService, AppConfig, _FakeEmbeddings]) -> None:
    """二进制文件按条跳过并计入 skipped，不该让整轮同步失败。"""
    service, config, _embeddings = env
    (_root(config) / "blob.bin").write_bytes(b"\x00\x01\x02\x03")

    summary = await service.sync_workspace()

    assert summary["skipped"] == 1, summary
    assert summary["items"][0]["source_path"] == "/blob.bin"


# --------------------------------------------------------------- 与全量索引互斥


async def test_manual_index_and_sync_do_not_interleave(env: tuple[KnowledgeService, AppConfig, _FakeEmbeddings]) -> None:
    """手动全量索引与自动同步共用一把锁：并发调用不会各自重复嵌入同一批文档。"""
    service, config, embeddings = env
    for index in range(3):
        _write(config, f"docs/{index}.md", f"# 文档 {index}\n\n正文 {index}")

    first, second = await asyncio.gather(service.index_workspace(), service.sync_workspace())

    assert first["indexed"] + second["indexed"] == 3, (first, second)
    # 两份汇总里「新索引」的总数正好等于文档数，说明没有哪份文档被两边各索引一次
    assert embeddings.embed_calls <= 6


__all__ = [
    "test_manual_index_and_sync_do_not_interleave",
    "test_sync_indexes_new_document",
    "test_sync_reindexes_changed_document",
    "test_sync_removes_deleted_document",
    "test_sync_second_run_does_not_reembed",
    "test_sync_skips_binary_document",
]
