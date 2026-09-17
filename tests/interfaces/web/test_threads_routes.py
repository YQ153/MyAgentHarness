"""/api/threads 的 HTTP 契约测试（清单过滤、重命名、归档）。

WHY 只挂业务路由而不起真实应用：``create_app`` 的 lifespan 会装配数据库、模型
与图，而本组用例要覆盖的是「查询参数翻译与状态码映射」，与真实依赖无关。

WHY 存储用替身而不是真库：``TestClient`` 在自己的事件循环里跑应用，测试侧另建的
aiosqlite 连接与锁跨循环使用必然报错；替身把断言聚焦在契约本身，而过滤语义
已由 ``tests/runtime/test_thread_store.py`` 与 ``tests/application/test_thread_service.py``
在真实存储上覆盖。
"""

from __future__ import annotations

from typing import Any

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.thread_service import ThreadService
from interfaces.web.routes import router
from tests.application.test_audit_enrichment import FakeCheckpointer, FakeGraphFactory
from tests.conftest import make_config


def _record(thread_id: str, title: str, *, archived: bool = False, owner_id: str = "") -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "owner_id": owner_id,
        "title": title,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "turn_count": 1,
        "archived": archived,
        "archived_at": "2026-01-02T00:00:00+00:00" if archived else "",
    }


class StubThreadStore:
    """会话元数据存储替身：在内存字典上实现路由需要的读写口，并记录入参。"""

    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self._records = {item["thread_id"]: item for item in (records or [])}
        self.list_calls: list[dict[str, Any]] = []

    async def get(self, thread_id: str) -> dict[str, Any] | None:
        return self._records.get(thread_id)

    async def list_threads(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.list_calls.append(kwargs)
        return list(self._records.values())

    async def count(self, **kwargs: Any) -> int:
        return len(self._records)

    async def rename(self, thread_id: str, title: str) -> dict[str, Any] | None:
        record = self._records.get(thread_id)
        if record is None:
            return None
        updated = {**record, "title": title}
        self._records[thread_id] = updated
        return updated

    async def set_archived(self, thread_id: str, archived: bool) -> dict[str, Any] | None:
        record = self._records.get(thread_id)
        if record is None:
            return None
        updated = {
            **record,
            "archived": archived,
            "archived_at": "2026-01-02T00:00:00+00:00" if archived else "",
        }
        self._records[thread_id] = updated
        return updated


class FailingRenameStore(StubThreadStore):
    """``rename`` 必定失败的替身：用于验证 500 映射。"""

    async def rename(self, thread_id: str, title: str) -> dict[str, Any] | None:
        raise aiosqlite.OperationalError("database disk image is malformed")


def _build_client(tmp_path, store: StubThreadStore | None) -> TestClient:
    """构造只挂载业务路由的测试客户端。"""
    app = FastAPI()
    config = make_config(tmp_path)
    app.state.config = config
    app.state.threads = (
        None
        if store is None
        else ThreadService(
            config,
            checkpointer=FakeCheckpointer(),
            thread_store=store,
            graph_factory=FakeGraphFactory(),
        )
    )
    app.include_router(router)
    return TestClient(app)


# ------------------------------------------------------------------ 清单过滤


def test_list_forwards_query_and_archive_scope(tmp_path):
    """查询参数必须落到存储层：否则界面上的搜索框只是个装饰。"""
    store = StubThreadStore([_record("t1", "排查登录超时")])
    client = _build_client(tmp_path, store)

    response = client.get("/api/threads?query=登录&include_archived=true&limit=10&offset=5")

    assert response.status_code == 200
    call = store.list_calls[0]
    assert call["query"] == "登录"
    assert call["include_archived"] is True
    assert call["limit"] == 10
    assert call["offset"] == 5


def test_list_defaults_to_no_filter(tmp_path):
    """不传参数时必须按「不过滤关键字、不含已归档」处理，而不是把 None 透下去。"""
    store = StubThreadStore([_record("t1", "会话")])
    client = _build_client(tmp_path, store)

    client.get("/api/threads")

    assert store.list_calls[0]["query"] is None
    assert store.list_calls[0]["include_archived"] is False


def test_list_response_carries_archive_fields(tmp_path):
    store = StubThreadStore([_record("t1", "会话", archived=True)])
    client = _build_client(tmp_path, store)

    item = client.get("/api/threads?include_archived=true").json()["items"][0]

    assert item["archived"] is True
    assert item["archived_at"]


def test_list_rejects_invalid_query(tmp_path):
    client = _build_client(tmp_path, StubThreadStore())

    response = client.get(f"/api/threads?query={'x' * 201}")

    assert response.status_code == 400
    assert "query 过长" in response.json()["detail"]


def test_list_returns_503_when_service_missing(tmp_path):
    client = _build_client(tmp_path, None)

    response = client.get("/api/threads")

    assert response.status_code == 503
    assert "会话服务未初始化" in response.json()["detail"]


# ------------------------------------------------------------------ 重命名与归档


def test_patch_renames_thread(tmp_path):
    store = StubThreadStore([_record("t1", "旧标题")])
    client = _build_client(tmp_path, store)

    response = client.patch("/api/threads/t1", json={"title": "  新标题  "})

    assert response.status_code == 200
    assert response.json()["title"] == "新标题"
    assert store._records["t1"]["title"] == "新标题"


def test_patch_archives_thread(tmp_path):
    store = StubThreadStore([_record("t1", "会话")])
    client = _build_client(tmp_path, store)

    response = client.patch("/api/threads/t1", json={"archived": True})

    assert response.status_code == 200
    assert response.json()["archived"] is True


def test_patch_applies_both_fields_in_one_request(tmp_path):
    """改完名顺手归档是同一个界面动作，不该让前端发两轮请求。"""
    store = StubThreadStore([_record("t1", "旧标题")])
    client = _build_client(tmp_path, store)

    response = client.patch("/api/threads/t1", json={"title": "新标题", "archived": True})

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "新标题"
    assert body["archived"] is True


def test_patch_rejects_empty_payload(tmp_path):
    """什么都没改的请求必须拒绝，否则会产出「成功但无变化」这种无法归因的结果。"""
    client = _build_client(tmp_path, StubThreadStore([_record("t1", "会话")]))

    response = client.patch("/api/threads/t1", json={})

    assert response.status_code == 400
    assert "至少要提供一项" in response.json()["detail"]


def test_patch_rejects_blank_title(tmp_path):
    client = _build_client(tmp_path, StubThreadStore([_record("t1", "会话")]))

    response = client.patch("/api/threads/t1", json={"title": "   "})

    assert response.status_code == 400
    assert "不能为空" in response.json()["detail"]


def test_patch_missing_thread_returns_404(tmp_path):
    client = _build_client(tmp_path, StubThreadStore())

    response = client.patch("/api/threads/ghost", json={"title": "新标题"})

    assert response.status_code == 404


def test_patch_rejects_invalid_thread_id(tmp_path):
    client = _build_client(tmp_path, StubThreadStore())

    response = client.patch("/api/threads/%20", json={"title": "新标题"})

    assert response.status_code == 400


def test_patch_storage_failure_returns_500(tmp_path):
    store = FailingRenameStore([_record("t1", "会话")])
    client = _build_client(tmp_path, store)

    response = client.patch("/api/threads/t1", json={"title": "新标题"})

    assert response.status_code == 500
    assert "重命名会话失败" in response.json()["detail"]
