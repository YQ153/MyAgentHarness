"""/api/memories 的 HTTP 契约测试。

WHY 只挂业务路由而不起真实应用：真实应用会在 lifespan 里装配图与 MCP，
而本组用例要验证的是「清单与删除如何呈现、非法路径如何映射」，不是装配本身。
"""

from __future__ import annotations

import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from agent.run_context import ANONYMOUS_USER_ID, memory_namespace
from application.memory_service import MemoryService
from interfaces.web.routes import router
from tests.conftest import make_config


async def _seed(
    store: InMemoryStore,
    key: str,
    content: str,
    owner: str = ANONYMOUS_USER_ID,
) -> None:
    await store.aput(
        memory_namespace(owner),
        key,
        {"content": content, "encoding": "utf-8"},
    )


def _build_client(tmp_path, service: MemoryService | None) -> TestClient:
    """构造只挂载业务路由的测试客户端。"""
    app = FastAPI()
    app.state.config = make_config(tmp_path)
    app.state.memories = service
    app.include_router(router)
    return TestClient(app)


def _service(tmp_path, store: InMemoryStore | None = None) -> MemoryService:
    return MemoryService(make_config(tmp_path), store=store if store is not None else InMemoryStore())


def test_list_memories_returns_virtual_paths(tmp_path):
    store = InMemoryStore()
    anyio.run(_seed, store, "/notes.md", "用户偏好中文")
    client = _build_client(tmp_path, _service(tmp_path, store))

    response = client.get("/api/memories")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["path"] == "/memories/notes.md"
    assert body["items"][0]["content"] == "用户偏好中文"
    assert body["total"] == 1
    assert body["truncated"] is False


def test_delete_memory_accepts_full_virtual_path(tmp_path):
    """前端直接把清单里的 path 拼在端点后面，因此必须收完整虚拟路径。"""
    store = InMemoryStore()
    anyio.run(_seed, store, "/notes.md", "待删除")
    client = _build_client(tmp_path, _service(tmp_path, store))

    response = client.delete("/api/memories/memories/notes.md")

    assert response.status_code == 200
    assert response.json() == {"path": "/memories/notes.md", "deleted": True}
    assert client.get("/api/memories").json()["items"] == []


def test_delete_memory_accepts_relative_path(tmp_path):
    store = InMemoryStore()
    anyio.run(_seed, store, "/notes.md", "待删除")
    client = _build_client(tmp_path, _service(tmp_path, store))

    response = client.delete("/api/memories/notes.md")

    assert response.status_code == 200
    assert response.json()["deleted"] is True


def test_delete_missing_memory_is_idempotent(tmp_path):
    client = _build_client(tmp_path, _service(tmp_path))

    response = client.delete("/api/memories/memories/ghost.md")

    assert response.status_code == 200
    assert response.json()["deleted"] is False


def test_memories_endpoint_returns_503_when_service_missing(tmp_path):
    client = _build_client(tmp_path, None)

    response = client.get("/api/memories")

    assert response.status_code == 503
    assert "记忆管理服务未初始化" in response.json()["detail"]
