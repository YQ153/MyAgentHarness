"""列表型配置项的校验与解析测试（MCP 服务器 / 自定义工具 / 环境白名单 / 技能目录）。

WHY 单独覆盖配置层：``pydantic-settings`` 对 ``list[...]`` 默认按 JSON 解码，
``SANDBOX_ENV_ALLOWLIST=PATH,TEMP`` 这类分隔符写法会在**加载期**抛一条与用户
意图无关的 JSON 解析错误——配置项写不进去，而报错信息完全指向别的方向。四个
列表型字段统一走 ``parse_list_config``，这里既覆盖各字段的自身校验（命令与
URL 二选一、scheme 匹配、重名），也覆盖「环境变量怎么写才生效」这一入口。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from config import AppConfig, MCPTransport, MCPServerSpec, parse_list_config
from tests.conftest import make_config


def _config_from_env(tmp_path: Path) -> AppConfig:
    """构造一个不显式传列表字段的配置，让环境变量有机会参与解析。

    WHY 不用 ``make_config``：它会显式传 ``skill_dirs``，而显式入参优先级高于
    环境变量——用它验证环境变量解析等于什么都没测到（断言拿到的是默认值，而
    测试还以为自己覆盖了环境变量）。
    """
    return AppConfig(
        _env_file=tmp_path / "does-not-exist.env",
        auth_mode="disabled",
        workspace=tmp_path / "workspace",
        memory_file=tmp_path / "workspace" / "AGENTS.md",
        db_path=tmp_path / "agent.db",
    )


def _with_servers(tmp_path: Path, *servers: dict[str, Any], **overrides: Any):
    """用给定服务器清单构造配置。

    WHY 统一走 ``make_config``：配置项校验与测试用的路径隔离必须同一套口径，
    否则「校验失败」可能只是因为工作区路径指向了不存在的目录。
    """
    return make_config(tmp_path, mcp_servers=list(servers), **overrides)


# ------------------------------------------------------------------ 传输校验


def test_stdio_transport_requires_command(tmp_path):
    """stdio 缺 command 时，若不在加载期报错，就只能在建连超时里看到它。"""
    with pytest.raises(ValidationError, match="command"):
        _with_servers(tmp_path, {"name": "srv", "transport": "stdio"})


@pytest.mark.parametrize("transport", ["sse", "streamable_http", "websocket"])
def test_url_transport_requires_url(tmp_path, transport):
    with pytest.raises(ValidationError, match="url"):
        _with_servers(tmp_path, {"name": "srv", "transport": transport})


@pytest.mark.parametrize(
    ("transport", "url"),
    [
        ("sse", "ws://mcp.example.com/sse"),
        ("streamable_http", "ftp://mcp.example.com/mcp"),
        ("websocket", "https://mcp.example.com/ws"),
    ],
)
def test_url_scheme_must_match_transport(tmp_path, transport, url):
    """scheme 写错时必须拦下：库会在建连阶段才失败，而那时配置早已生效。"""
    with pytest.raises(ValidationError):
        _with_servers(tmp_path, {"name": "srv", "transport": transport, "url": url})


def test_websocket_accepts_wss(tmp_path):
    config = _with_servers(
        tmp_path, {"name": "srv", "transport": "websocket", "url": "wss://mcp.example.com/ws"}
    )

    assert config.mcp_servers[0].transport is MCPTransport.WEBSOCKET


def test_transport_is_normalized_before_validation():
    """`` STDIO `` 与 ``stdio`` 等价，与 ``execution_mode`` 保持同一口径。"""
    spec = MCPServerSpec(name="srv", transport=" STDIO ", command="python")

    assert spec.transport is MCPTransport.STDIO


def test_unknown_transport_is_rejected():
    with pytest.raises(ValidationError):
        MCPServerSpec(name="srv", command="python", transport="carrier-pigeon")


def test_unknown_field_is_rejected():
    """拼错的字段名必须报错，否则表现为「配置写了但没生效」。"""
    with pytest.raises(ValidationError):
        MCPServerSpec(name="srv", transport="stdio", commnad="python")


def test_server_name_is_restricted():
    """名字同时是日志标识、审计字段与工具前缀，特殊字符会让前缀匹配失效。"""
    with pytest.raises(ValidationError):
        MCPServerSpec(name="my server", transport="stdio", command="python")


def test_duplicate_server_names_rejected(tmp_path):
    """重名会让「这条工具来自哪台服务器」失去答案。"""
    with pytest.raises(ValidationError, match="重复"):
        _with_servers(
            tmp_path,
            {"name": "dup", "transport": "stdio", "command": "python"},
            {"name": "dup", "transport": "stdio", "command": "python"},
        )


# ------------------------------------------------------------------ 启用开关


def test_active_servers_respects_per_server_switch(tmp_path):
    config = _with_servers(
        tmp_path,
        {"name": "on", "transport": "stdio", "command": "python"},
        {"name": "off", "transport": "stdio", "command": "python", "enabled": False},
    )

    assert [spec.name for spec in config.active_mcp_servers()] == ["on"]


def test_active_servers_respects_global_switch(tmp_path):
    """总开关关闭时保留配置但一台都不连——排障时最需要的就是这个。"""
    config = _with_servers(
        tmp_path,
        {"name": "on", "transport": "stdio", "command": "python"},
        mcp_enabled=False,
    )

    assert config.active_mcp_servers() == []
    assert [spec.name for spec in config.mcp_servers] == ["on"]


# ------------------------------------------------------------------ 工具模块


def test_custom_tool_modules_accepts_comma_separated_env(tmp_path, monkeypatch):
    """``CUSTOM_TOOL_MODULES=a.b,c.d`` 必须能用：点分模块名的自然写法就是逗号。"""
    monkeypatch.setenv("CUSTOM_TOOL_MODULES", "a.b, c.d")

    assert make_config(tmp_path).custom_tool_modules == ["a.b", "c.d"]


def test_custom_tool_modules_accepts_json_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CUSTOM_TOOL_MODULES", '["a.b","c.d"]')

    assert make_config(tmp_path).custom_tool_modules == ["a.b", "c.d"]


def test_custom_tool_modules_drops_blank_items(tmp_path, monkeypatch):
    """空项要剔除：拿空串去 import 只会得到一条与用户意图无关的 ImportError。"""
    monkeypatch.setenv("CUSTOM_TOOL_MODULES", "a.b, ,c.d,")

    assert make_config(tmp_path).custom_tool_modules == ["a.b", "c.d"]


def test_custom_tool_modules_accepts_empty_env(tmp_path, monkeypatch):
    """``CUSTOM_TOOL_MODULES=""`` 是 shell 里「清空变量」的常见写法，不等于配错。"""
    monkeypatch.setenv("CUSTOM_TOOL_MODULES", "")

    assert make_config(tmp_path).custom_tool_modules == []


def test_custom_tool_modules_rejects_broken_json(tmp_path, monkeypatch):
    """以 ``[`` 开头却解析不了时必须报错，而不是退化成「一个模块名」后去 import。"""
    monkeypatch.setenv("CUSTOM_TOOL_MODULES", '["a.b"')

    with pytest.raises(ValidationError, match="JSON"):
        make_config(tmp_path)


def test_custom_tool_modules_rejects_non_string_items(tmp_path, monkeypatch):
    """静默丢弃非字符串项会让「写了三个实际装了两个」变得不可见。"""
    monkeypatch.setenv("CUSTOM_TOOL_MODULES", "[1, 2]")

    with pytest.raises(ValidationError, match="custom_tool_modules"):
        make_config(tmp_path)


def test_mcp_servers_env_json_is_still_parsed(tmp_path, monkeypatch):
    """回归保护：``custom_tool_modules`` 关掉默认解码后，MCP 清单仍须走 JSON。"""
    monkeypatch.setenv(
        "MCP_SERVERS",
        '[{"name": "srv", "transport": "stdio", "command": "python"}]',
    )

    config = make_config(tmp_path)

    assert [spec.name for spec in config.mcp_servers] == ["srv"]
    assert config.mcp_servers[0].command == "python"


# ------------------------------------------------------------------ 环境白名单


def test_env_allowlist_keeps_safe_defaults(tmp_path):
    """默认值不含 ``USERPROFILE`` / ``HOME``——它们会引导子进程去读用户凭据。"""
    allowlist = make_config(tmp_path).sandbox_env_allowlist

    assert "PATH" in allowlist
    assert "USERPROFILE" not in allowlist
    assert "HOME" not in allowlist


def test_env_allowlist_accepts_comma_separated_env(tmp_path, monkeypatch):
    """README 给的 ``SANDBOX_ENV_ALLOWLIST=PATH,SYSTEMROOT,...`` 必须真的能生效。"""
    monkeypatch.setenv("SANDBOX_ENV_ALLOWLIST", "PATH, TEMP ,TZ")

    assert make_config(tmp_path).sandbox_env_allowlist == ["PATH", "TEMP", "TZ"]


def test_env_allowlist_accepts_json_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_ENV_ALLOWLIST", '["PATH","TEMP"]')

    assert make_config(tmp_path).sandbox_env_allowlist == ["PATH", "TEMP"]


def test_env_allowlist_rejects_broken_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_ENV_ALLOWLIST", '["PATH"')

    with pytest.raises(ValidationError, match="JSON"):
        make_config(tmp_path)


def test_env_allowlist_rejects_non_string_items(tmp_path, monkeypatch):
    """白名单写成数字会让过滤静默失效——没有变量能匹配上 ``1``。"""
    monkeypatch.setenv("SANDBOX_ENV_ALLOWLIST", "[1, 2]")

    with pytest.raises(ValidationError, match="sandbox_env_allowlist"):
        make_config(tmp_path)


# ------------------------------------------------------------------ 技能目录


def test_skill_dirs_accepts_path_separated_env(tmp_path, monkeypatch):
    """与 ``PATH`` 同口径：路径本身可能含逗号，不能用逗号当分隔符。"""
    monkeypatch.setenv(
        "SKILL_DIRS", os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])
    )

    assert [path.name for path in _config_from_env(tmp_path).skill_dirs] == ["a", "b"]


def test_skill_dirs_accepts_json_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_DIRS", json.dumps([str(tmp_path / "a"), str(tmp_path / "b")]))

    assert [path.name for path in _config_from_env(tmp_path).skill_dirs] == ["a", "b"]


def test_skill_dirs_keeps_path_containing_comma(tmp_path, monkeypatch):
    """含逗号的目录名必须整体保留——这正是 ``skill_dirs`` 不用逗号切的理由。"""
    monkeypatch.setenv("SKILL_DIRS", str(tmp_path / "has,comma"))

    assert [path.name for path in _config_from_env(tmp_path).skill_dirs] == ["has,comma"]


def test_skill_dirs_drops_blank_items(tmp_path, monkeypatch):
    """尾部多写一个分隔符（``a;``）在 shell 里很常见，不该解析出一个空路径。"""
    monkeypatch.setenv("SKILL_DIRS", os.pathsep.join([str(tmp_path / "a"), "", "  "]))

    assert [path.name for path in _config_from_env(tmp_path).skill_dirs] == ["a"]


# ------------------------------------------------------------------ 解析函数


def test_parse_list_config_rejects_empty_separators():
    with pytest.raises(ValueError, match="分隔符"):
        parse_list_config("a", field="x", separators=())


def test_parse_list_config_rejects_scalar():
    with pytest.raises(ValueError, match="必须是字符串或序列"):
        parse_list_config(42, field="x", separators=(",",))


def test_parse_list_config_preserves_sequence_order():
    """顺序即优先级（技能目录），不能因为转 list 或去重而改变。"""
    assert parse_list_config(("b", "a"), field="x", separators=(",",)) == ["b", "a"]


def test_parse_list_config_normalizes_none_and_blank():
    assert parse_list_config(None, field="x", separators=(",",)) == []
    assert parse_list_config("   ", field="x", separators=(",",)) == []
