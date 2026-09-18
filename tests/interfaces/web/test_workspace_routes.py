"""工作区文件面板端点的 HTTP 契约测试。

WHY 只挂业务路由而不起真实应用：``create_app`` 的 lifespan 会装配数据库、模型与图，
而本组用例要覆盖的是「查询参数翻译与失败状态码映射」，与真实依赖无关。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.workspace_service import WorkspaceService
from interfaces.web.workspace_routes import router
from tests.conftest import make_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    root = tmp_path / "workspace"
    (root / "react-vite-app").mkdir(parents=True)
    (root / "react-vite-app" / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    config = make_config(tmp_path)
    app = FastAPI()
    app.state.config = config
    app.state.workspace = WorkspaceService(config)
    app.include_router(router)
    return TestClient(app)


def test_lists_workspace_root(client: TestClient):
    response = client.get("/api/workspace/files")

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "/"
    assert body["parent"] is None
    assert [item["name"] for item in body["entries"]] == ["react-vite-app"]


def test_lists_subdirectory(client: TestClient):
    response = client.get("/api/workspace/files", params={"path": "/react-vite-app"})

    assert response.status_code == 200
    body = response.json()
    assert body["parent"] == "/"
    assert body["entries"][0]["path"] == "/react-vite-app/index.html"
    assert body["entries"][0]["is_dir"] is False


def test_reads_file_content(client: TestClient):
    response = client.get(
        "/api/workspace/file", params={"path": "/react-vite-app/index.html"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "text"
    assert body["name"] == "index.html"
    assert "<h1>hi</h1>" in body["text"]
    assert body["truncated"] is False


def test_missing_target_returns_404(client: TestClient):
    assert client.get("/api/workspace/files", params={"path": "/nope"}).status_code == 404
    assert client.get("/api/workspace/file", params={"path": "/nope.txt"}).status_code == 404


@pytest.mark.parametrize("path", ["/../outside", "/~/secret", "/C:/Windows/win.ini"])
def test_illegal_path_returns_400(client: TestClient, path: str):
    """越界与非法路径统一映射成 400：对调用方而言都是「这个 path 不被接受」，
    再细分只会额外告诉探测者「哪个路径真实存在」。"""
    assert client.get("/api/workspace/files", params={"path": path}).status_code == 400
    assert client.get("/api/workspace/file", params={"path": path}).status_code == 400
