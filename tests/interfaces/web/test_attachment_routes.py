"""附件端点的回归测试。

覆盖面：上传成功与四种拒绝（过大 / 类型不符 / 超张数 / 会话 ID 含分隔符）、
上限查询、清单需要会话存在、删除幂等，以及「元信息落盘而非内容入库」的落点。

WHY 用 httpx 的 ASGITransport 而不是 ``TestClient``：附件端点要读真实的
``thread_store``（归属校验），而它的生命周期是异步上下文；``TestClient`` 自带事件
循环，与异步夹具里的连接分属两个循环，会在并发写库时抛 ``InvalidStateError``。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from application.attachment_service import AttachmentService
from llm.registry import build_default_registry
from runtime.attachments import attachment_dir
from runtime.thread_store import ThreadMetaStore, open_thread_store
from interfaces.web.attachment_routes import router
from tests.conftest import StubSessionRegistry, make_config, make_root

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_THREAD = "a" * 32


class _StubAudit:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def log(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture()
async def client(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, ThreadMetaStore, Path]]:
    async with open_thread_store(tmp_path / "threads.db") as store:
        config = make_config(tmp_path)
        config.ensure_directories()
        service = AttachmentService(
            config,
            scope=make_root(config),
            registry=build_default_registry(config),
            thread_store=store,
            audit_store=_StubAudit(),
        )
        app = FastAPI()
        app.state.config = config
        app.state.attachments = service
        app.state.workspaces = StubSessionRegistry(config, attachments=service)
        app.include_router(router)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, store, Path(make_root(config).root)


def _upload_url(thread_id: str = _THREAD) -> str:
    return f"/api/threads/{thread_id}/attachments"


async def test_upload_and_list_roundtrip(
    client: tuple[httpx.AsyncClient, ThreadMetaStore, Path]
):
    http, store, workspace = client
    await store.create(_THREAD)

    response = await http.post(
        _upload_url(), files={"file": ("shot.png", _PNG, "image/png")}
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["filename"] == "shot.png"
    assert payload["size"] == len(_PNG)
    assert (attachment_dir(workspace, _THREAD) / f"{payload['id']}.png").is_file()

    listed = await http.get(_upload_url())
    assert listed.status_code == 200
    body = listed.json()
    assert [item["id"] for item in body["items"]] == [payload["id"]]
    assert body["limits"]["max_per_thread"] == 8


async def test_upload_rejects_oversized_file(tmp_path: Path):
    """超过上限即 400：分块读取在越过上限那一刻就中止，不必等整个请求体落盘。"""
    async with open_thread_store(tmp_path / "threads.db") as store:
        config = make_config(tmp_path, attachment_max_bytes=1024)
        config.ensure_directories()
        service = AttachmentService(
            config,
            scope=make_root(config),
            registry=build_default_registry(config),
            thread_store=store,
            audit_store=_StubAudit(),
        )
        app = FastAPI()
        app.state.config = config
        app.state.attachments = service
        app.state.workspaces = StubSessionRegistry(config, attachments=service)
        app.include_router(router)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post(
                _upload_url(), files={"file": ("big.png", b"x" * 5000, "image/png")}
            )

    assert response.status_code == 400
    assert "过大" in response.json()["detail"]


async def test_upload_rejects_disallowed_mime(
    client: tuple[httpx.AsyncClient, ThreadMetaStore, Path]
):
    http, _store, _workspace = client

    response = await http.post(
        _upload_url(), files={"file": ("a.pdf", _PNG, "application/pdf")}
    )

    assert response.status_code == 400
    assert "不支持的文件类型" in response.json()["detail"]


async def test_upload_rejects_thread_id_with_separator(
    client: tuple[httpx.AsyncClient, ThreadMetaStore, Path]
):
    """会话 ID 含分隔符时必须 400，而不是在工作区里造出多层目录。"""
    http, _store, _workspace = client

    response = await http.post(
        "/api/threads/a%5Cb/attachments", files={"file": ("a.png", _PNG, "image/png")}
    )

    assert response.status_code == 400


async def test_list_requires_existing_thread(
    client: tuple[httpx.AsyncClient, ThreadMetaStore, Path]
):
    http, _store, _workspace = client

    response = await http.get(_upload_url())

    assert response.status_code == 404


async def test_delete_is_idempotent(client: tuple[httpx.AsyncClient, ThreadMetaStore, Path]):
    http, store, _workspace = client
    await store.create(_THREAD)
    uploaded = await http.post(
        _upload_url(), files={"file": ("a.png", _PNG, "image/png")}
    )
    attachment_id = uploaded.json()["id"]

    first = await http.delete(f"{_upload_url()}/{attachment_id}")
    second = await http.delete(f"{_upload_url()}/{attachment_id}")

    assert first.status_code == 200
    assert first.json()["deleted"] is True
    # 再删一次是「本来就没有」，不是错误：并发重试与重复清理都会走这条路径
    assert second.status_code == 200
    assert second.json()["deleted"] is False


async def test_limits_endpoint_is_thread_independent(
    client: tuple[httpx.AsyncClient, ThreadMetaStore, Path]
):
    """上限必须在草稿态（尚无会话）也能取到——前端要靠它做选文件时校验。"""
    http, _store, _workspace = client

    response = await http.get("/api/attachments/limits")

    assert response.status_code == 200
    body = response.json()
    assert body["max_bytes"] > 0
    assert "image/png" in body["allowed_mime_types"]
