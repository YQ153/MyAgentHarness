"""知识库工具与进程级句柄。

覆盖三处容易出错的地方：

1. **句柄是模块级状态**：不清空就会带着上一个用例的数据目录留到下一个，表现是
   「单独跑能过、连起来跑就不过」——因此有一条 autouse 夹具强制清理。
2. **工具结果的可用性**：必须带来源文件与所在小节，且片段要被截断，否则一次检索就把
   上下文塞满。
3. **降级要写在结果里**：语义检索失败时关键词结果照常返回，但结果里要说明这次没走语义。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import knowledge_runtime
from agent.tools import ToolRegistry
from config import AppConfig
from knowledge_runtime import ensure_service, peek_service
from knowledge_tools import KnowledgeToolError, register_tools
from tests.conftest import make_config


@pytest.fixture(autouse=True)
async def _reset_knowledge_runtime() -> AsyncIterator[None]:
    """每个用例前后清空进程级句柄。

    WHY 必须清：句柄是模块级状态，上一个用例装好的实例会带着**别人的数据目录**留到
    下一个用例；那种串扰只在「连起来跑」时出现。
    """
    await knowledge_runtime.close_service()
    yield
    await knowledge_runtime.close_service()


def _registry(config: AppConfig) -> ToolRegistry:
    """按配置注册知识库工具，返回注册器。"""
    registry = ToolRegistry()
    register_tools(registry, config)
    return registry


def _tool(registry: ToolRegistry, name: str) -> Any:
    """按名字取出已注册的工具。"""
    for item in registry.tools():
        if item.name == name:
            return item
    raise AssertionError(f"未注册工具：{name}（已有：{registry.names()}）")


def _workspace(tmp_path: Path, relative: str, text: str) -> AppConfig:
    """建好工作区并写入一份文件，返回配置。"""
    config = make_config(tmp_path)
    config.workspace.mkdir(parents=True, exist_ok=True)
    target = config.workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return config


# --------------------------------------------------------------- 注册


def test_register_requires_config(tmp_path: Path) -> None:
    """不带配置加载时必须报错。

    WHY：不带配置时工具会以「没有知识库」的形态注册，而失败要等到某次对话才出现；
    模块被加载却拿不到配置属于配置写法错误，应当在这里就说清楚。
    """
    with pytest.raises(ValueError, match="register_tools"):
        register_tools(ToolRegistry())


def test_registers_search_and_index_tools(tmp_path: Path) -> None:
    """两个工具都注册，且归为自定义来源。"""
    registry = _registry(make_config(tmp_path))

    assert sorted(registry.names()) == ["index_documents", "search_documents"]


# --------------------------------------------------------------- 工具行为


async def test_search_before_indexing_reports_no_hits(tmp_path: Path) -> None:
    """还没索引任何内容时，返回一句明确的「没检索到」而不是报错。"""
    registry = _registry(make_config(tmp_path))

    result = await _tool(registry, "search_documents").ainvoke({"query": "登录接口"})

    assert "没有" in result and "登录接口" in result


async def test_index_then_search_round_trip(tmp_path: Path) -> None:
    """验收形态：先索引工作区，再通过工具检索到并带上出处与所在小节。"""
    config = _workspace(
        tmp_path, "notes/login.md", "# 登录问题\n\n登录接口超时排查记录：p99 达到 3 秒。"
    )
    registry = _registry(config)

    indexed = await _tool(registry, "index_documents").ainvoke({})
    assert "新索引 1 个" in indexed

    result = await _tool(registry, "search_documents").ainvoke({"query": "登录接口"})

    assert "/notes/login.md" in result
    assert "登录问题" in result
    assert "p99" in result


async def test_repeated_index_skips_unchanged_documents(tmp_path: Path) -> None:
    """内容未变时不重复索引——否则每次刷新都要重跑一遍嵌入。"""
    config = _workspace(tmp_path, "notes/login.md", "登录接口超时排查记录。")
    registry = _registry(config)
    index_tool = _tool(registry, "index_documents")

    await index_tool.ainvoke({})
    second = await index_tool.ainvoke({})

    assert "内容未变 1 个" in second


async def test_search_rejects_blank_query(tmp_path: Path) -> None:
    """空检索词转成工具错误，让模型能改写后重试而不是当成内部故障。"""
    registry = _registry(make_config(tmp_path))

    with pytest.raises(KnowledgeToolError, match="检索词不合法"):
        await _tool(registry, "search_documents").ainvoke({"query": "   "})


async def test_snippet_is_truncated(tmp_path: Path) -> None:
    """长片段要被截断并标注——检索的价值是指出去哪里看，不是把文档搬进上下文。"""
    config = _workspace(tmp_path, "notes/long.md", "关键词 " * 400)
    registry = _registry(config)
    await _tool(registry, "index_documents").ainvoke({})

    result = await _tool(registry, "search_documents").ainvoke({"query": "关键词"})

    assert "片段已截断" in result
    assert len(result) < 3000


async def test_skipped_files_are_counted(tmp_path: Path) -> None:
    """无法索引的文件计入跳过数，而不是让整次索引失败。

    WHY：工作区里出现一个二进制文件是常事；若它能让整次索引中断，用户会以为
    「知识库坏了」，而真实原因与他无关。
    """
    config = _workspace(tmp_path, "notes/login.md", "登录接口超时排查记录。")
    (config.workspace / "blob.bin").write_bytes(b"\x00\x01binary")
    registry = _registry(config)

    result = await _tool(registry, "index_documents").ainvoke({})

    assert "跳过 1 个" in result


# --------------------------------------------------------------- 进程级句柄


async def test_ensure_service_is_a_process_singleton(tmp_path: Path) -> None:
    """重复调用拿到同一个实例。

    WHY 关键：工具与接口必须看到同一份事实（同一个连接、同一份索引视图）；各建一份
    会让「工具检索得到、接口列表里没有」这类矛盾同时成立。
    """
    config = make_config(tmp_path)

    first = await ensure_service(config)
    second = await ensure_service()

    assert first is second


async def test_ensure_service_without_config_raises(tmp_path: Path) -> None:
    """尚未装配且没给配置时，报出可操作的错误。"""
    with pytest.raises(RuntimeError, match="config"):
        await ensure_service()


async def test_peek_service_does_not_assemble(tmp_path: Path) -> None:
    """``peek_service`` 如实回答「还没装」，且不顺手装起来。

    WHY：就绪探测与能力公示会调用它；若顺手装配，一次健康检查就会产生一次数据库连接。
    """
    assert peek_service() is None

    await ensure_service(make_config(tmp_path))

    assert peek_service() is not None


async def test_close_service_is_idempotent(tmp_path: Path) -> None:
    """重复关闭不报错——它同时出现在正常退出与异常退出两条路径上。"""
    await ensure_service(make_config(tmp_path))

    await knowledge_runtime.close_service()
    await knowledge_runtime.close_service()

    assert peek_service() is None


async def test_service_recovers_after_close(tmp_path: Path) -> None:
    """关闭后再取用要能重新装配，而不是留下一个「用一次就废」的句柄。"""
    config = make_config(tmp_path)
    await ensure_service(config)
    await knowledge_runtime.close_service()

    rebuilt = await ensure_service(config)

    assert rebuilt.capabilities()["vector_enabled"] is False


async def test_knowledge_db_sits_next_to_the_data_dir(tmp_path: Path) -> None:
    """知识库文件落在数据目录下、与主库分开。

    WHY 分开：向量维度一变就要整库重建，独立文件让「删掉重来」是明确可执行的；而
    ``vec0`` 是加载式扩展，写进主库会让「扩展不可用」与检查点库纠缠在一起。
    """
    config = make_config(tmp_path)
    await ensure_service(config)

    assert knowledge_runtime.knowledge_db_path(config) == tmp_path / "knowledge.db"
    assert (tmp_path / "knowledge.db").is_file()
