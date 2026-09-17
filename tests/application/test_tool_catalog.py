"""工具目录服务的单元测试。

WHY 单独覆盖目录而不只测注册器：目录是「对外可见的工具事实」，接口与审计
都从它取值；注册器正确但目录漏项（例如忘记把内置工具算进去）时，运维看到
的清单仍然会骗人。
"""

from __future__ import annotations

import pytest

from agent.mcp import MCPServerStatus
from agent.tooling import ToolBundle
from agent.tools import BUILTIN_TOOL_NAMES, ToolDescriptor, ToolSource
from application.tool_catalog import ToolCatalog


def _bundle(
    *descriptors: ToolDescriptor,
    custom_modules: tuple[str, ...] = (),
    mcp_statuses: tuple[MCPServerStatus, ...] = (),
) -> ToolBundle:
    """构造一个只含描述的装配结果（本用例不关心可执行工具对象）。"""
    return ToolBundle(
        tools=(),
        descriptors=descriptors,
        custom_modules=custom_modules,
        mcp_statuses=mcp_statuses,
    )


def test_catalog_rejects_none_bundle():
    with pytest.raises(ValueError, match="bundle"):
        ToolCatalog(None)


def test_list_tools_puts_builtin_first_in_fixed_order():
    catalog = ToolCatalog(
        _bundle(
            ToolDescriptor(
                name="srv_weather",
                source=ToolSource.MCP,
                description="查天气",
                server="srv",
            ),
            ToolDescriptor(name="translate", source=ToolSource.CUSTOM, description="翻译"),
        )
    )

    result = catalog.list_tools()

    builtin = [item.name for item in result.items if item.source == "builtin"]
    assert builtin[:3] == ["write_todos", "ls", "read_file"]
    assert set(builtin) == set(BUILTIN_TOOL_NAMES)
    # 内置在前、扩展在后：扩展工具的名字里常带服务器前缀，混排会难读
    assert [item.source for item in result.items] == ["builtin"] * len(builtin) + [
        "mcp",
        "custom",
    ]
    assert result.total == len(result.items)


def test_list_tools_carries_extension_metadata():
    catalog = ToolCatalog(
        _bundle(
            ToolDescriptor(
                name="srv_weather",
                source=ToolSource.MCP,
                description="查天气",
                server="srv",
            )
        )
    )

    extension = [item for item in catalog.list_tools().items if item.source != "builtin"][0]

    assert extension.name == "srv_weather"
    assert extension.description == "查天气"
    assert extension.server == "srv"


def test_list_tools_reports_custom_modules_and_mcp_status():
    catalog = ToolCatalog(
        _bundle(
            custom_modules=("demo_tools",),
            mcp_statuses=(
                MCPServerStatus(name="good", transport="stdio", ok=True, tool_count=2),
                MCPServerStatus(name="bad", transport="sse", ok=False, error="RuntimeError: 超时"),
            ),
        )
    )

    result = catalog.list_tools()

    assert result.custom_modules == ["demo_tools"]
    assert [item.name for item in result.mcp_servers] == ["good", "bad"]
    assert result.mcp_servers[0].tool_count == 2
    assert result.mcp_servers[1].ok is False
    # 失败原因必须透出：否则「工具没出现」在接口上表现为一片空白
    assert "超时" in result.mcp_servers[1].error


def test_source_of_distinguishes_origin():
    catalog = ToolCatalog(
        _bundle(
            ToolDescriptor(name="srv_weather", source=ToolSource.MCP, server="srv"),
            ToolDescriptor(name="translate", source=ToolSource.CUSTOM),
        )
    )

    assert catalog.source_of("read_file") == "builtin"
    assert catalog.source_of("srv_weather") == "mcp"
    assert catalog.source_of("translate") == "custom"
    # 旧配置里残留的工具名要如实标为未知，不能被误读成内置能力
    assert catalog.source_of("removed_tool") == "unknown"


def test_server_of_only_for_mcp_tools():
    catalog = ToolCatalog(
        _bundle(ToolDescriptor(name="srv_weather", source=ToolSource.MCP, server="srv"))
    )

    assert catalog.server_of("srv_weather") == "srv"
    assert catalog.server_of("read_file") is None
