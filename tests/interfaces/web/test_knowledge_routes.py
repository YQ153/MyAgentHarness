"""知识库端点的回归测试。

覆盖面：空清单、工作区索引与单文档索引、两类拒绝（二进制 400 / 文件不存在 404）、
内容未变与强制重建的区别、移除幂等、路径穿越拦截。

WHY 用 httpx 的 ASGITransport 而不是 ``TestClient``：与附件端点同一理由——这些用例要
读真实的 ``KnowledgeStore``（异步连接），而 ``TestClient`` 自带事件循环，与异步夹具里
的连接分属两个循环，会在并发访问时抛 ``InvalidStateError``。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from application.knowledge_service import KnowledgeService
from config import AppConfig
from interfaces.web.knowledge_routes import router
from runtime.knowledge_store import open_knowledge_store
from tests.conftest import make_config

_DIMS = 4


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, AppConfig]]:
    """只挂知识库路由的应用（与真实启动共用同一个服务实现）。"""
    config = make_config(tmp_path)
    config.workspace.mkdir(parents=True, exist_ok=True)
    async with open_knowledge_store(
        tmp_path / "knowledge.db", dims=_DIMS, model="", vector_enabled=False
    ) as store:
        app = FastAPI()
        app.state.config = config
        app.state.knowledge = KnowledgeService(config, store=store)
        app.include_router(router)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, config


def _write(config: AppConfig, relative: str, text: str) -> str:
    """在工作区里写一份文件，返回其虚拟路径。"""
    target = config.workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return "/" + relative


# --------------------------------------------------------------- 清单


async def test_list_reports_capabilities_on_empty_index(
    api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """空索引也返回能力与统计，前端据此决定展示什么。"""
    http, _ = api

    response = await http.get("/api/knowledge")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["stats"]["document_count"] == 0
    assert body["capabilities"]["vector_enabled"] is False
    assert body["capabilities"]["top_k"] >= 1


# --------------------------------------------------------------- 索引


async def test_index_workspace_then_list(api: tuple[httpx.AsyncClient, AppConfig]) -> None:
    """索引整个工作区，随后清单里能看到文档与分块数。"""
    http, config = api
    _write(config, "notes/login.md", "# 登录问题\n\n登录接口超时排查记录。")

    indexed = await http.post("/api/knowledge", json={})
    assert indexed.status_code == 200
    summary = indexed.json()
    assert (summary["scanned"], summary["indexed"]) == (1, 1)
    assert summary["items"][0]["source_path"] == "/notes/login.md"

    listed = await http.get("/api/knowledge")
    items = listed.json()["items"]
    assert [item["source_path"] for item in items] == ["/notes/login.md"]
    assert items[0]["chunk_count"] >= 1


async def test_index_single_document_by_path(api: tuple[httpx.AsyncClient, AppConfig]) -> None:
    """带 ``path`` 时只索引那一份，未被指定的文件不进索引。"""
    http, config = api
    _write(config, "notes/login.md", "登录接口超时排查记录。")
    _write(config, "notes/other.md", "另一份文档。")

    response = await http.post("/api/knowledge", json={"path": "/notes/login.md"})

    assert response.status_code == 200
    assert response.json()["scanned"] == 1
    listed = await http.get("/api/knowledge")
    assert [item["source_path"] for item in listed.json()["items"]] == ["/notes/login.md"]


async def test_unchanged_document_is_skipped_then_forced(
    api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """内容未变时计入 ``unchanged``；``force`` 时重新索引。

    WHY 这条值得单独测：它区分了「按内容指纹判重」与「按时间戳判重」——后者在检出、
    复制、同步之后会做大量无意义的重复嵌入。
    """
    http, config = api
    _write(config, "notes/login.md", "登录接口超时排查记录。")
    await http.post("/api/knowledge", json={})

    second = await http.post("/api/knowledge", json={})
    assert second.json()["unchanged"] == 1

    forced = await http.post("/api/knowledge", json={"force": True})
    assert forced.json()["indexed"] == 1


async def test_binary_file_is_rejected_with_400(
    api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """显式指定一份二进制文件时返回 400 并说明原因。"""
    http, config = api
    (config.workspace / "blob.bin").write_bytes(b"\x00\x01binary")

    response = await http.post("/api/knowledge", json={"path": "/blob.bin"})

    assert response.status_code == 400
    assert "二进制" in response.json()["detail"]


async def test_binary_file_is_skipped_when_indexing_workspace(
    api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """整区索引时二进制文件按条跳过并给出原因，而不是让整次请求失败。

    WHY：工作区里出现一个二进制文件是常事；它若能让整次索引失败，用户会以为
    「知识库坏了」，而真实原因与他无关。
    """
    http, config = api
    _write(config, "notes/login.md", "登录接口超时排查记录。")
    (config.workspace / "blob.bin").write_bytes(b"\x00\x01binary")

    response = await http.post("/api/knowledge", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["indexed"] == 1
    assert body["skipped"] == 1
    skipped = [item for item in body["items"] if item["status"] == "skipped"]
    assert "二进制" in skipped[0]["detail"]


async def test_missing_file_returns_404(api: tuple[httpx.AsyncClient, AppConfig]) -> None:
    """指定的文件不存在时 404（与 400「文件不适合索引」区分开）。"""
    http, _ = api

    response = await http.post("/api/knowledge", json={"path": "/nope.md"})

    assert response.status_code == 404


async def test_traversal_path_is_rejected(api: tuple[httpx.AsyncClient, AppConfig]) -> None:
    """逃出工作区的路径返回 400，而不是 500。"""
    http, _ = api

    response = await http.post("/api/knowledge", json={"path": "/../outside.md"})

    assert response.status_code == 400


# --------------------------------------------------------------- 移除


async def test_delete_removes_document_from_index(
    api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """移除后清单里不再出现，工作区源文件不受影响。"""
    http, config = api
    _write(config, "notes/login.md", "登录接口超时排查记录。")
    await http.post("/api/knowledge", json={})

    deleted = await http.delete("/api/knowledge", params={"path": "/notes/login.md"})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    listed = await http.get("/api/knowledge")
    assert listed.json()["items"] == []
    assert (config.workspace / "notes/login.md").is_file(), "源文件不该被删"


async def test_delete_is_idempotent(api: tuple[httpx.AsyncClient, AppConfig]) -> None:
    """再次移除只是 ``deleted=false``，不是 404。

    WHY：移除的目标状态是「它不在索引里」，而不是「它曾经在」。已达成目标状态时
    再要求调用方区分「本来就没有」，只会逼前端自己先查一次清单。
    """
    http, _ = api

    response = await http.delete("/api/knowledge", params={"path": "/nope.md"})

    assert response.status_code == 200
    assert response.json()["deleted"] is False


async def test_delete_rejects_traversal_path(
    api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """含上跳片段的路径被拦下。"""
    http, _ = api

    response = await http.delete("/api/knowledge", params={"path": "/../outside.md"})

    assert response.status_code == 400
