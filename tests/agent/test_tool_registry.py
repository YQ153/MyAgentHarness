"""工具注册器与自定义工具模块加载的单元测试。

WHY 冲突判定是重点：工具名是模型可见的全局命名空间，一个重名的扩展工具
不会报错，只会让内置工具「消失」——这类故障在对话层面表现为「Agent 突然
不会读文件了」，与代码改动的因果链很长。因此这里把每种冲突都钉成用例。
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from langchain_core.tools import BaseTool, StructuredTool

from agent.tooling import build_tool_bundle
from agent.tools import (
    BUILTIN_TOOL_NAMES,
    CustomToolModuleError,
    ToolNameConflictError,
    ToolRegistry,
    ToolSource,
    load_custom_tool_modules,
)
from tests.conftest import make_config


def _echo_tool(name: str, description: str = "") -> BaseTool:
    """构造一个最小可用工具（名字可控）。"""

    def _echo(text: str) -> str:
        """原样回显。"""
        return text

    return StructuredTool.from_function(
        func=_echo,
        name=name,
        description=description or f"{name} 工具",
    )


def _module(name: str, **attributes: Any) -> types.ModuleType:
    """构造一个已注册到 ``sys.modules`` 的假模块。"""
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


# ------------------------------------------------------------------ 注册基础


def test_register_records_source_and_description():
    registry = ToolRegistry()

    registry.register(_echo_tool("summarize", "总结文本"), source=ToolSource.CUSTOM)

    assert registry.names() == ["summarize"]
    assert len(registry) == 1
    descriptor = registry.descriptors()[0]
    assert descriptor.name == "summarize"
    assert descriptor.source is ToolSource.CUSTOM
    assert descriptor.description == "总结文本"
    assert descriptor.server is None


def test_register_keeps_registration_order():
    """WHY 断言顺序：工具清单是给人和模型看的，顺序稳定才能对比两次启动。"""
    registry = ToolRegistry()

    for name in ("alpha", "beta", "gamma"):
        registry.register(_echo_tool(name))

    assert registry.names() == ["alpha", "beta", "gamma"]
    assert [item.name for item in registry.descriptors()] == ["alpha", "beta", "gamma"]


def test_register_accepts_plain_callable_with_inferred_schema():
    registry = ToolRegistry()

    def word_count(text: str) -> int:
        """统计词数。"""
        return len(text.split())

    tool = registry.register(word_count, name="word_count")

    assert isinstance(tool, BaseTool)
    assert tool.name == "word_count"
    # 参数 schema 来自类型注解，说明自定义工具与内置工具共享参数校验
    assert list(tool.args_schema.model_json_schema()["properties"]) == ["text"]


def test_register_tool_decorator_returns_tool():
    registry = ToolRegistry()

    @registry.register_tool(name="shout", description="转大写")
    def shout(text: str) -> str:
        return text.upper()

    assert shout.name == "shout"
    assert registry.names() == ["shout"]


# ------------------------------------------------------------------ 冲突判定


@pytest.mark.parametrize("builtin_name", sorted(BUILTIN_TOOL_NAMES))
def test_register_rejects_builtin_name_collision(builtin_name: str):
    """WHY 逐个内置名参数化：漏掉任何一个都等于给「静默顶替内置工具」留口子。"""
    registry = ToolRegistry()

    with pytest.raises(ToolNameConflictError) as excinfo:
        registry.register(_echo_tool(builtin_name))

    assert "内置工具" in str(excinfo.value)
    assert registry.names() == []


def test_register_rejects_duplicate_extension_name():
    registry = ToolRegistry()
    registry.register(_echo_tool("weather"), source=ToolSource.MCP, server="srv-a")

    with pytest.raises(ToolNameConflictError) as excinfo:
        registry.register(_echo_tool("weather"), source=ToolSource.MCP, server="srv-b")

    # 报错必须点明原来的归属，否则运维不知道要改哪台服务器的配置
    assert "srv-a" in str(excinfo.value)
    assert registry.names() == ["weather"]


def test_register_mcp_tool_requires_server_name():
    registry = ToolRegistry()

    with pytest.raises(ValueError, match="server"):
        registry.register(_echo_tool("weather"), source=ToolSource.MCP)


def test_register_rejects_invalid_inputs():
    registry = ToolRegistry()

    with pytest.raises(ValueError, match="None"):
        registry.register(None)
    with pytest.raises(ValueError, match="BaseTool"):
        registry.register("not-a-tool")
    with pytest.raises(ValueError, match="ToolSource"):
        registry.register(_echo_tool("x"), source="custom")
    with pytest.raises(ValueError, match="工具名"):
        registry.register(_echo_tool("   "))


def test_registry_rejects_none_builtin_names():
    with pytest.raises(ValueError, match="builtin_names"):
        ToolRegistry(builtin_names=None)


# ------------------------------------------------------------------ 模块加载


def test_load_custom_modules_from_tools_attribute(monkeypatch):
    registry = ToolRegistry()
    module = _module("demo_tools", TOOLS=[_echo_tool("alpha"), _echo_tool("beta")])
    monkeypatch.setitem(sys.modules, "demo_tools", module)

    loaded = load_custom_tool_modules(["demo_tools"], registry)

    assert loaded == ["demo_tools"]
    assert registry.names() == ["alpha", "beta"]
    assert all(item.source is ToolSource.CUSTOM for item in registry.descriptors())


def test_load_custom_modules_from_register_hook(monkeypatch):
    registry = ToolRegistry()

    def register_tools(target: ToolRegistry) -> None:
        target.register(_echo_tool("from-hook"))

    module = _module("hook_tools", register_tools=register_tools)
    monkeypatch.setitem(sys.modules, "hook_tools", module)

    load_custom_tool_modules(["hook_tools"], registry)

    assert registry.names() == ["from-hook"]


# ------------------------------------------------------------------ 配置传入


def test_load_custom_modules_passes_config_to_two_arg_hook(monkeypatch):
    """WHY 覆盖这条：模块若看不到配置，就只能自己去读环境变量或重解析 ``.env``，
    而这两条路都会绕开 ``config.py`` 这个唯一解析点。"""
    registry = ToolRegistry()
    seen: list[Any] = []

    def register_tools(target: ToolRegistry, config: Any) -> None:
        seen.append(config)
        target.register(_echo_tool("configured"))

    monkeypatch.setitem(
        sys.modules,
        "configured_tools",
        _module("configured_tools", register_tools=register_tools),
    )
    sentinel = object()

    load_custom_tool_modules(["configured_tools"], registry, config=sentinel)

    assert seen == [sentinel]
    assert registry.names() == ["configured"]


def test_load_custom_modules_keeps_single_arg_hook_working_with_config(monkeypatch):
    """向后兼容：既有模块写的是单参钩子，而装配侧现在会传配置——它不能被搞坏。

    这是本次扩展最重要的回归点：扩展点必须加法演进，否则所有已部署的模块
    都会在升级后变成「加载失败」。"""
    registry = ToolRegistry()

    def register_tools(target: ToolRegistry) -> None:
        target.register(_echo_tool("legacy"))

    monkeypatch.setitem(
        sys.modules,
        "legacy_tools",
        _module("legacy_tools", register_tools=register_tools),
    )

    load_custom_tool_modules(["legacy_tools"], registry, config=object())

    assert registry.names() == ["legacy"]


def test_load_custom_modules_passes_config_to_variadic_hook(monkeypatch):
    """``*args`` 形态的钩子同样应拿到配置：它接受位置参数，只是没写名字。"""
    registry = ToolRegistry()
    seen: list[Any] = []

    def register_tools(*args: Any) -> None:
        seen.append(args[1])
        args[0].register(_echo_tool("variadic"))

    monkeypatch.setitem(
        sys.modules,
        "variadic_tools",
        _module("variadic_tools", register_tools=register_tools),
    )
    sentinel = object()

    load_custom_tool_modules(["variadic_tools"], registry, config=sentinel)

    assert seen == [sentinel]
    assert registry.names() == ["variadic"]


def test_load_custom_modules_defaults_config_to_none(monkeypatch):
    """不传配置时钩子收到的是 ``None``，而不是因为缺参而加载失败。"""
    registry = ToolRegistry()
    seen: list[Any] = []

    def register_tools(target: ToolRegistry, config: Any) -> None:
        seen.append(config)
        target.register(_echo_tool("no-config"))

    monkeypatch.setitem(
        sys.modules,
        "no_config_tools",
        _module("no_config_tools", register_tools=register_tools),
    )

    load_custom_tool_modules(["no_config_tools"], registry)

    assert seen == [None]


def test_load_custom_modules_can_register_nothing(monkeypatch):
    """模块按条件不注册任何工具是合法结果（例如缺密钥就该整体缺席），不应报错。"""
    registry = ToolRegistry()

    def register_tools(target: ToolRegistry, config: Any) -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "conditional_tools",
        _module("conditional_tools", register_tools=register_tools),
    )

    loaded = load_custom_tool_modules(["conditional_tools"], registry, config=object())

    assert loaded == ["conditional_tools"]
    assert registry.names() == []


def test_load_custom_modules_raises_on_import_error():
    """WHY 断言异常类型：导入失败若被吞掉，用户只会看到「工具少了」。"""
    registry = ToolRegistry()

    with pytest.raises(CustomToolModuleError, match="导入失败"):
        load_custom_tool_modules(["definitely_not_a_module_xyz"], registry)


def test_load_custom_modules_raises_without_entrypoint(monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setitem(sys.modules, "empty_tools", _module("empty_tools"))

    with pytest.raises(CustomToolModuleError, match="TOOLS"):
        load_custom_tool_modules(["empty_tools"], registry)


def test_load_custom_modules_rejects_none_registry():
    with pytest.raises(ValueError, match="registry"):
        load_custom_tool_modules(["whatever"], None)


def test_load_custom_modules_rejects_blank_name():
    registry = ToolRegistry()

    with pytest.raises(CustomToolModuleError, match="非空字符串"):
        load_custom_tool_modules(["   "], registry)


def test_load_custom_modules_conflict_is_raised(monkeypatch):
    """模块里写了与内置同名的工具时，加载必须立刻失败。"""
    registry = ToolRegistry()
    monkeypatch.setitem(sys.modules, "bad_tools", _module("bad_tools", TOOLS=[_echo_tool("grep")]))

    with pytest.raises(ToolNameConflictError):
        load_custom_tool_modules(["bad_tools"], registry)


# ------------------------------------------------------------------ 装配


async def test_build_tool_bundle_without_extensions_is_empty(tmp_path):
    bundle = await build_tool_bundle(make_config(tmp_path))

    assert bundle.tools == ()
    assert bundle.descriptors == ()
    assert bundle.custom_modules == ()
    assert bundle.mcp_enabled is False


async def test_build_tool_bundle_loads_custom_module(tmp_path, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "bundle_tools",
        _module("bundle_tools", TOOLS=[_echo_tool("translate")]),
    )
    config = make_config(tmp_path, custom_tool_modules=["bundle_tools"])

    bundle = await build_tool_bundle(config)

    assert [tool.name for tool in bundle.tools] == ["translate"]
    assert bundle.custom_modules == ("bundle_tools",)
    assert bundle.server_of("translate") is None


async def test_build_tool_bundle_passes_config_to_custom_module(tmp_path, monkeypatch):
    """WHY 需要这条集成用例：扩展点支持了配置而装配侧忘了往下传，等于没扩展。"""
    holder: dict[str, Any] = {}

    def register_tools(target: ToolRegistry, config: Any) -> None:
        holder["config"] = config
        target.register(_echo_tool("web_search"))

    monkeypatch.setitem(
        sys.modules,
        "webish_tools",
        _module("webish_tools", register_tools=register_tools),
    )
    config = make_config(tmp_path, custom_tool_modules=["webish_tools"])

    bundle = await build_tool_bundle(config)

    assert holder["config"] is config
    assert [tool.name for tool in bundle.tools] == ["web_search"]


async def test_build_tool_bundle_rejects_none_config():
    with pytest.raises(ValueError, match="config"):
        await build_tool_bundle(None)


async def test_build_tool_bundle_skips_mcp_when_disabled(tmp_path):
    """总开关关闭时不应产生任何连接尝试。"""
    config = make_config(
        tmp_path,
        mcp_enabled=False,
        mcp_servers=[{"name": "srv", "transport": "stdio", "command": "python"}],
    )

    bundle = await build_tool_bundle(config)

    assert bundle.mcp_statuses == ()
    assert bundle.tools == ()
