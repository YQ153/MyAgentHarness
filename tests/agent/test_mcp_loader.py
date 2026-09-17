"""MCP 加载器的单元测试。

WHY 用假客户端而不是真起一个 MCP server：真 server 只能验证「能连上」，
而生产里真正难复现的是「某台服务器超时」「某台握手失败」——这两种情况
决定了降级策略是否正确。假客户端可以用一个 ``plan`` 精确制造这些分支，
且不依赖任何外部进程。
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest
from langchain_core.tools import BaseTool, StructuredTool

from agent import mcp as mcp_module
from agent.mcp import MCPLoadError, MCPToolLoader
from agent.tooling import build_tool_bundle
from agent.tools import ToolNameConflictError
from tests.conftest import make_config


def _tool(name: str) -> BaseTool:
    """构造一个最小工具。"""

    def _echo(text: str) -> str:
        """原样回显。"""
        return text

    return StructuredTool.from_function(func=_echo, name=name, description=f"{name} 工具")


class FakeMCPClient:
    """``MultiServerMCPClient`` 替身。

    ``plan`` 的取值语义：``list`` 表示返回这些工具，``float`` 表示先睡再返回
    空列表（用于制造超时），``Exception`` 表示抛出（用于制造握手失败）。
    """

    plan: dict[str, Any] = {}
    instances: list[FakeMCPClient] = []

    def __init__(self, connections: dict[str, Any], *, tool_name_prefix: bool = False) -> None:
        self.connections = dict(connections)
        self.tool_name_prefix = tool_name_prefix
        self.requested: list[str] = []
        type(self).instances.append(self)

    async def get_tools(self, *, server_name: str) -> list[BaseTool]:
        """按 plan 返回工具。"""
        self.requested.append(server_name)
        planned = type(self).plan.get(server_name)
        if isinstance(planned, Exception):
            raise planned
        if isinstance(planned, float):
            await asyncio.sleep(planned)
            return []
        return list(planned or [])


@pytest.fixture(autouse=True)
def _reset_plan() -> None:
    """每个用例前清空 plan 与实例记录，避免上一个用例的数据泄漏过来。"""
    FakeMCPClient.plan = {}
    FakeMCPClient.instances = []


def _patch_client(monkeypatch) -> None:
    """把加载器使用的客户端换成替身。"""
    monkeypatch.setattr(mcp_module, "MultiServerMCPClient", FakeMCPClient)


def _fast_timeout(monkeypatch, seconds: float = 0.01) -> None:
    """把加载器里引用的 ``asyncio`` 换成只改超时的替身。

    WHY 替换模块引用而不是改 ``asyncio`` 本身：后者会影响同一瞬间其它
    协程的超时语义；只替换 ``agent.mcp`` 持有的那个引用，影响面收敛在本
    模块内。同时保留原始 ``wait_for``，测的仍是真实的超时路径。
    """
    real_wait_for = asyncio.wait_for

    def _wait_for(coro, timeout, **kwargs):  # noqa: ANN001, ANN202 - 透传包装
        return real_wait_for(coro, seconds, **kwargs)

    monkeypatch.setattr(
        mcp_module,
        "asyncio",
        types.SimpleNamespace(wait_for=_wait_for, CancelledError=asyncio.CancelledError),
    )


# ------------------------------------------------------------------ 连接翻译


def test_build_connections_maps_stdio_fields(tmp_path):
    config = make_config(
        tmp_path,
        mcp_servers=[
            {
                "name": "filesystem",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                "env": {"TOKEN": "secret"},
                "cwd": str(tmp_path),
            }
        ],
    )

    connections = MCPToolLoader(config).build_connections()

    assert connections["filesystem"]["transport"] == "stdio"
    assert connections["filesystem"]["command"] == "npx"
    assert connections["filesystem"]["args"][0] == "-y"
    assert connections["filesystem"]["cwd"] == str(tmp_path)
    # WHY 断言环境变量「只有」配置里那一个：stdio server 不该继承宿主环境，
    # 否则宿主机的 API Key 会被第三方进程读走。
    assert connections["filesystem"]["env"] == {"TOKEN": "secret"}


@pytest.mark.parametrize(
    ("transport", "url", "expected"),
    [
        ("sse", "https://mcp.example.com/sse", "sse"),
        ("streamable_http", "https://mcp.example.com/mcp", "streamable_http"),
        ("websocket", "wss://mcp.example.com/ws", "websocket"),
    ],
)
def test_build_connections_maps_url_transports(tmp_path, transport, url, expected):
    config = make_config(
        tmp_path,
        mcp_servers=[
            {"name": "remote", "transport": transport, "url": url, "headers": {"X-Token": "t"}}
        ],
    )

    connections = MCPToolLoader(config).build_connections()

    assert connections["remote"]["transport"] == expected
    assert connections["remote"]["url"] == url
    assert connections["remote"]["headers"] == {"X-Token": "t"}


def test_build_connections_skips_disabled_server(tmp_path):
    config = make_config(
        tmp_path,
        mcp_servers=[
            {"name": "on", "transport": "stdio", "command": "python"},
            {"name": "off", "transport": "stdio", "command": "python", "enabled": False},
        ],
    )

    assert list(MCPToolLoader(config).build_connections()) == ["on"]


def test_loader_rejects_none_config():
    with pytest.raises(ValueError, match="config"):
        MCPToolLoader(None)


# ------------------------------------------------------------------ 加载


async def test_load_without_servers_returns_empty(tmp_path):
    result = await MCPToolLoader(make_config(tmp_path)).load()

    assert result.tools == ()
    assert result.statuses == ()
    assert result.ok is True


async def test_load_collects_tools_and_status(tmp_path, monkeypatch):
    _patch_client(monkeypatch)
    FakeMCPClient.plan = {"srv-a": [_tool("srv-a_echo")]}
    config = make_config(
        tmp_path,
        mcp_servers=[{"name": "srv-a", "transport": "stdio", "command": "python"}],
    )

    result = await MCPToolLoader(config).load()

    assert [tool.name for tool in result.tools] == ["srv-a_echo"]
    assert result.statuses[0].name == "srv-a"
    assert result.statuses[0].ok is True
    assert result.statuses[0].tool_count == 1
    assert result.statuses[0].state == "ok"


async def test_load_isolates_failing_server(tmp_path, monkeypatch):
    """WHY 本用例是 MCP 接入的核心约束：一台第三方服务器挂掉，不能让其余
    服务器的工具一起消失——一次性拉取全部服务器的实现正是这样丢失工具的。"""
    _patch_client(monkeypatch)
    FakeMCPClient.plan = {
        "bad": RuntimeError("握手失败"),
        "good": [_tool("good_echo")],
    }
    config = make_config(
        tmp_path,
        mcp_servers=[
            {"name": "bad", "transport": "stdio", "command": "python"},
            {"name": "good", "transport": "stdio", "command": "python"},
        ],
    )

    result = await MCPToolLoader(config).load()

    assert [tool.name for tool in result.tools] == ["good_echo"]
    failed = result.failed
    assert [item.name for item in failed] == ["bad"]
    assert "握手失败" in failed[0].error
    assert "RuntimeError" in failed[0].error
    assert result.ok is False


async def test_load_raises_when_fail_fast(tmp_path, monkeypatch):
    _patch_client(monkeypatch)
    FakeMCPClient.plan = {"bad": RuntimeError("握手失败")}
    config = make_config(
        tmp_path,
        mcp_fail_fast=True,
        mcp_servers=[{"name": "bad", "transport": "stdio", "command": "python"}],
    )

    with pytest.raises(MCPLoadError) as excinfo:
        await MCPToolLoader(config).load()

    assert "bad" in str(excinfo.value)


async def test_load_marks_timeout_as_failure(tmp_path, monkeypatch):
    """不响应握手的 server 必须因超时被判失败，而不是让装配挂死。"""
    _patch_client(monkeypatch)
    _fast_timeout(monkeypatch)
    FakeMCPClient.plan = {"slow": 5.0}
    config = make_config(
        tmp_path,
        mcp_servers=[{"name": "slow", "transport": "stdio", "command": "python"}],
    )

    result = await MCPToolLoader(config).load()

    assert result.tools == ()
    assert result.statuses[0].ok is False
    assert "TimeoutError" in result.statuses[0].error


async def test_load_propagates_cancellation(tmp_path, monkeypatch):
    """WHY 断言取消语义：装配被取消意味着进程正在退出，把它记成
    「这台服务器失败了」会掩盖真正的退出原因。

    WHY 直接替换 ``wait_for`` 而不是让假客户端抛 ``CancelledError``：后者
    会被 ``wait_for`` 自身的取消处理吸收成别的异常，测到的是标准库行为
    而不是本模块的分支；本用例要验证的是「取消原样上抛」，直接注入即可。
    """
    _patch_client(monkeypatch)

    def _cancelled(coro, timeout, **kwargs):  # noqa: ANN001, ANN202 - 透传包装
        coro.close()
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        mcp_module,
        "asyncio",
        types.SimpleNamespace(wait_for=_cancelled, CancelledError=asyncio.CancelledError),
    )
    config = make_config(
        tmp_path,
        mcp_servers=[{"name": "srv", "transport": "stdio", "command": "python"}],
    )

    with pytest.raises(asyncio.CancelledError):
        await MCPToolLoader(config).load()


async def test_load_passes_tool_name_prefix_flag(tmp_path, monkeypatch):
    """前缀开关必须真的传给客户端，否则「冲突显式化」的承诺是假的。"""
    _patch_client(monkeypatch)
    config = make_config(
        tmp_path,
        mcp_tool_name_prefix=False,
        mcp_servers=[{"name": "srv", "transport": "stdio", "command": "python"}],
    )

    await MCPToolLoader(config).load()

    assert FakeMCPClient.instances[0].tool_name_prefix is False


# ------------------------------------------------------------------ 装配归属


async def test_bundle_registers_mcp_tools_with_server(tmp_path, monkeypatch):
    _patch_client(monkeypatch)
    FakeMCPClient.plan = {"srv-a": [_tool("srv-a_weather")]}
    config = make_config(
        tmp_path,
        mcp_servers=[{"name": "srv-a", "transport": "stdio", "command": "python"}],
    )

    bundle = await build_tool_bundle(config)

    assert [tool.name for tool in bundle.tools] == ["srv-a_weather"]
    assert bundle.server_of("srv-a_weather") == "srv-a"
    assert bundle.mcp_enabled is True


async def test_bundle_falls_back_to_generic_server_name(tmp_path, monkeypatch):
    """关掉前缀且有多台服务器时，归属只能标为 ``mcp``——宁可模糊也不能空。"""
    _patch_client(monkeypatch)
    FakeMCPClient.plan = {"srv-a": [_tool("alpha")], "srv-b": [_tool("beta")]}
    config = make_config(
        tmp_path,
        mcp_tool_name_prefix=False,
        mcp_servers=[
            {"name": "srv-a", "transport": "stdio", "command": "python"},
            {"name": "srv-b", "transport": "stdio", "command": "python"},
        ],
    )

    bundle = await build_tool_bundle(config)

    assert bundle.server_of("alpha") == "mcp"
    assert bundle.server_of("beta") == "mcp"


async def test_bundle_rejects_duplicate_mcp_tool_names(tmp_path, monkeypatch):
    """两台服务器提供同名工具时必须显式报错，而不是后者静默覆盖前者。"""
    _patch_client(monkeypatch)
    FakeMCPClient.plan = {"srv-a": [_tool("echo")], "srv-b": [_tool("echo")]}
    config = make_config(
        tmp_path,
        mcp_tool_name_prefix=False,
        mcp_servers=[
            {"name": "srv-a", "transport": "stdio", "command": "python"},
            {"name": "srv-b", "transport": "stdio", "command": "python"},
        ],
    )

    with pytest.raises(ToolNameConflictError):
        await build_tool_bundle(config)
