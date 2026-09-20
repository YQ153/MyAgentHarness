"""/api/tools 的 HTTP 契约测试。

WHY 只挂业务路由而不起真实应用：真实应用会在 lifespan 里连接 MCP server，
而本端点要验证的是「装配结果如何呈现」，不是「能不能连上第三方服务」。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.mcp import MCPServerStatus
from agent.tooling import ToolBundle
from agent.tools import BUILTIN_TOOL_NAMES, ToolDescriptor, ToolSource
from application.tool_catalog import ToolCatalog
from interfaces.web.routes import router
from tests.conftest import make_config


def _catalog() -> ToolCatalog:
    """构造一个含扩展工具与失败服务器的目录。"""
    return ToolCatalog(
        ToolBundle(
            tools=(),
            descriptors=(
                ToolDescriptor(
                    name="srv_weather",
                    source=ToolSource.MCP,
                    description="查天气",
                    server="srv",
                ),
                ToolDescriptor(name="translate", source=ToolSource.CUSTOM, description="翻译"),
            ),
            custom_modules=("demo_tools",),
            # 替身与真实装配结果同形：内置工具名随 bundle 一起交出（见 Q9）
            builtin_names=BUILTIN_TOOL_NAMES,
            mcp_statuses=(
                MCPServerStatus(name="srv", transport="stdio", ok=True, tool_count=1),
                MCPServerStatus(
                    name="down",
                    transport="streamable_http",
                    ok=False,
                    error="RuntimeError: 连接被拒绝",
                ),
            ),
        )
    )


def _build_client(tmp_path, catalog: ToolCatalog | None) -> TestClient:
    """构造只挂载业务路由的测试客户端。"""
    app = FastAPI()
    app.state.config = make_config(tmp_path)
    app.state.tools = catalog
    app.include_router(router)
    return TestClient(app)


def test_tools_endpoint_lists_builtin_and_extensions(tmp_path):
    client = _build_client(tmp_path, _catalog())

    response = client.get("/api/tools")

    assert response.status_code == 200
    body = response.json()
    sources = {item["name"]: item["source"] for item in body["items"]}
    assert sources["read_file"] == "builtin"
    assert sources["srv_weather"] == "mcp"
    assert sources["translate"] == "custom"
    assert body["total"] == len(BUILTIN_TOOL_NAMES) + 2
    assert body["custom_modules"] == ["demo_tools"]


def test_tools_endpoint_exposes_mcp_failure(tmp_path):
    """失败服务器必须在响应里可见，否则「工具少了」只能靠猜。"""
    client = _build_client(tmp_path, _catalog())

    servers = client.get("/api/tools").json()["mcp_servers"]

    assert [item["name"] for item in servers] == ["srv", "down"]
    assert servers[0]["ok"] is True
    assert servers[1]["ok"] is False
    assert "连接被拒绝" in servers[1]["error"]


def test_tools_endpoint_returns_503_when_catalog_missing(tmp_path):
    client = _build_client(tmp_path, None)

    response = client.get("/api/tools")

    assert response.status_code == 503
    assert "工具目录未初始化" in response.json()["detail"]


def test_models_endpoint_requires_config(tmp_path):
    """配置缺失时端点应报 503 而不是 500。"""
    app = FastAPI()
    app.state.tools = _catalog()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/models")

    assert response.status_code == 503
