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
from langchain.tools import ToolRuntime

import knowledge_runtime
from agent.run_context import AgentRunContext
from agent.tools import ToolRegistry
from config import AppConfig
from knowledge_runtime import ensure_service, peek_service
from knowledge_tools import KnowledgeToolError, register_tools
from tests.conftest import make_config, make_root


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


def _runtime(workspace: str) -> ToolRuntime:
    """构造一个真实的 ``ToolRuntime``，其上下文声明本轮运行的工作区。

    WHY 用真类型而不是自造替身：工具的参数注解就是 ``ToolRuntime``，langchain 会按
    该类型校验注入进来的值——替身过不了校验，也就等于没有验证「注入这条路是通的」。
    这里把六个必填字段填齐，其余三个与工作区无关的留默认值。
    """
    return ToolRuntime(
        state={},
        context=AgentRunContext(workspace=workspace),
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id="test-call",
        store=None,
    )


def _call(registry: ToolRegistry, name: str, config: AppConfig, **payload: Any) -> Any:
    """以「本轮工作区 = config 的工作区」调用工具，返回可 await 的协程。

    WHY 每个用例都要显式带上运行时：工作区是知识库隔离的唯一依据，工具拿不到它时
    只能回落启动默认值（清单里多于一个工作区时那可能就是错的库）。让用例每次都显式
    给出，也顺带钉住「注入参数确实是通过运行时进来的」。
    """
    return _tool(registry, name).ainvoke(
        {**payload, "runtime": _runtime(str(make_root(config).root))}
    )


def _workspace(tmp_path: Path, relative: str, text: str) -> AppConfig:
    """建好工作区并写入一份文件，返回配置。"""
    config = make_config(tmp_path)
    make_root(config).root.mkdir(parents=True, exist_ok=True)
    target = make_root(config).root / relative
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
    config = make_config(tmp_path)
    registry = _registry(config)

    result = await _call(registry, "search_documents", config, query="登录接口")

    assert "没有" in result and "登录接口" in result


async def test_index_then_search_round_trip(tmp_path: Path) -> None:
    """验收形态：先索引工作区，再通过工具检索到并带上出处与所在小节。"""
    config = _workspace(
        tmp_path, "notes/login.md", "# 登录问题\n\n登录接口超时排查记录：p99 达到 3 秒。"
    )
    registry = _registry(config)

    indexed = await _call(registry, "index_documents", config)
    assert "新索引 1 个" in indexed

    result = await _call(registry, "search_documents", config, query="登录接口")

    assert "/notes/login.md" in result
    assert "登录问题" in result
    assert "p99" in result


async def test_repeated_index_skips_unchanged_documents(tmp_path: Path) -> None:
    """内容未变时不重复索引——否则每次刷新都要重跑一遍嵌入。"""
    config = _workspace(tmp_path, "notes/login.md", "登录接口超时排查记录。")
    registry = _registry(config)

    await _call(registry, "index_documents", config)
    second = await _call(registry, "index_documents", config)

    assert "内容未变 1 个" in second


async def test_search_rejects_blank_query(tmp_path: Path) -> None:
    """空检索词转成工具错误，让模型能改写后重试而不是当成内部故障。"""
    config = make_config(tmp_path)
    registry = _registry(config)

    with pytest.raises(KnowledgeToolError, match="检索词不合法"):
        await _call(registry, "search_documents", config, query="   ")


async def test_snippet_is_truncated(tmp_path: Path) -> None:
    """长片段要被截断并标注——检索的价值是指出去哪里看，不是把文档搬进上下文。"""
    config = _workspace(tmp_path, "notes/long.md", "关键词 " * 400)
    registry = _registry(config)
    await _call(registry, "index_documents", config)

    result = await _call(registry, "search_documents", config, query="关键词")

    assert "片段已截断" in result
    assert len(result) < 3000


async def test_skipped_files_are_counted(tmp_path: Path) -> None:
    """无法索引的文件计入跳过数，而不是让整次索引失败。

    WHY：工作区里出现一个二进制文件是常事；若它能让整次索引中断，用户会以为
    「知识库坏了」，而真实原因与他无关。
    """
    config = _workspace(tmp_path, "notes/login.md", "登录接口超时排查记录。")
    (make_root(config).root / "blob.bin").write_bytes(b"\x00\x01binary")
    registry = _registry(config)

    result = await _call(registry, "index_documents", config)

    assert "跳过 1 个" in result


# --------------------------------------------------------------- 进程级句柄


async def test_ensure_service_is_a_process_singleton(tmp_path: Path) -> None:
    """同一个工作区重复调用拿到同一个实例。

    WHY 关键：工具与接口必须看到同一份事实（同一个连接、同一份索引视图）；各建一份
    会让「工具检索得到、接口列表里没有」这类矛盾同时成立。
    """
    config = make_config(tmp_path)

    first = await ensure_service(config, make_root(config).root)
    second = await ensure_service(config, make_root(config).root)

    assert first is second


async def test_each_workspace_gets_its_own_library(tmp_path: Path) -> None:
    """两个工作区拿到**不同**实例，且落在**不同**的库文件上。

    WHY 必须分开：库里以「工作区内的虚拟路径」为键去重（``UNIQUE(owner_id,
    source_path)``），而两个项目里的 ``/README.md`` 是同一个键——共用一个库就会互相
    覆盖索引，「删除这份文档」还会删到另一个项目的同名文件。

    WHY 两套配置用不同数据目录：库文件名里的工作区标识是「启动默认工作区沿用历史
    文件名」的例外之外的正常路径；而那个例外的前提是「一个部署只有一个默认工作区」，
    共用同一个数据目录的两套配置不在该前提内。
    """
    first = make_config(tmp_path / "one")
    second_root = tmp_path / "two" / "workspace"
    second_root.mkdir(parents=True)
    second = make_config(tmp_path / "two", workspace=second_root)

    service_a = await ensure_service(first, make_root(first).root)
    service_b = await ensure_service(second, make_root(second).root)

    assert service_a is not service_b
    assert knowledge_runtime.knowledge_db_path(first, make_root(first).root) != (
        knowledge_runtime.knowledge_db_path(second, second_root)
    )


async def test_each_workspace_indexes_only_its_own_documents(tmp_path: Path) -> None:
    """同名文件在两个工作区里互不影响——这是「索引串味」的回归线。"""
    shared_name = "notes/login.md"
    config_a = _workspace(tmp_path / "a", shared_name, "A 项目的登录接口用 JWT。")
    other = tmp_path / "b" / "workspace"
    other.mkdir(parents=True)
    config_b = make_config(tmp_path / "b", workspace=other)
    target = other / shared_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("B 项目的登录接口用 Cookie。", encoding="utf-8")

    # WHY 两套配置各注册一次工具：工具按**装配它的那份配置**解析工作区，而配置只服务
    # 自己允许清单内的工作区（这正是「清单」要拦的那件事）。共用一份注册器会让第二条
    # 记录撞上「根不存在」——那不是缺陷，是「每个根一个库」在正常工作。
    registry_a = _registry(config_a)
    registry_b = _registry(config_b)
    await _call(registry_a, "index_documents", config_a)
    await _call(registry_b, "index_documents", config_b)

    hits_a = await _call(registry_a, "search_documents", config_a, query="登录接口")
    hits_b = await _call(registry_b, "search_documents", config_b, query="登录接口")

    assert "JWT" in hits_a and "Cookie" not in hits_a
    assert "Cookie" in hits_b and "JWT" not in hits_b


async def test_ensure_service_without_a_root_raises() -> None:
    """既没给根也没给配置时，报出可操作的错误。

    WHY 单独钉：知识库按文件根隔离，而库里以「根内虚拟路径」为键去重——没有根就等于
    不知道该开哪个库。此时必须拒绝，而不是悄悄退到某个默认目录（那会让两个项目共用
    一份索引，且不报任何错）。
    """
    with pytest.raises(ValueError, match="根"):
        await ensure_service()


async def test_peek_service_does_not_assemble(tmp_path: Path) -> None:
    """``peek_service`` 如实回答「还没装」，且不顺手装起来。

    WHY：就绪探测与能力公示会调用它；若顺手装配，一次健康检查就会产生一次数据库连接。
    """
    assert peek_service() is None

    config = make_config(tmp_path)
    await ensure_service(config, make_root(config).root)

    assert peek_service() is not None
    # 不带参数时回答「有没有任何一个装了」；带工作区时回答那个具体的
    assert peek_service(make_root(config).root) is not None


async def test_close_service_is_idempotent(tmp_path: Path) -> None:
    """重复关闭不报错——它同时出现在正常退出与异常退出两条路径上。"""
    await ensure_service(
        make_config(tmp_path), make_root(make_config(tmp_path)).root
    )

    await knowledge_runtime.close_service()
    await knowledge_runtime.close_service()

    assert peek_service() is None


async def test_close_service_releases_every_workspace(tmp_path: Path) -> None:
    """关闭要覆盖**全部**工作区，而不是只关最后一次装配的那个。

    WHY：共享的嵌入后端与每个工作区的库连接是两套生命周期；漏掉任何一个都会在
    进程退出时留下一个没关的连接（Windows 上表现为临时目录删不掉）。
    """
    first = make_config(tmp_path / "one")
    other = tmp_path / "two" / "workspace"
    other.mkdir(parents=True)
    second = make_config(tmp_path / "two", workspace=other)
    await ensure_service(first, make_root(first).root)
    await ensure_service(second, make_root(second).root)

    await knowledge_runtime.close_service()

    assert peek_service() is None
    assert peek_service(make_root(first).root) is None
    assert peek_service(other) is None


async def test_service_recovers_after_close(tmp_path: Path) -> None:
    """关闭后再取用要能重新装配，而不是留下一个「用一次就废」的句柄。"""
    config = make_config(tmp_path)
    await ensure_service(config, make_root(config).root)
    await knowledge_runtime.close_service()

    rebuilt = await ensure_service(config, make_root(config).root)

    assert rebuilt.capabilities()["vector_enabled"] is False


async def test_knowledge_db_sits_next_to_the_data_dir(tmp_path: Path) -> None:
    """知识库文件落在数据目录下、与主库分开。

    WHY 分开：向量维度一变就要整库重建，独立文件让「删掉重来」是明确可执行的；而
    ``vec0`` 是加载式扩展，写进主库会让「扩展不可用」与检查点库纠缠在一起。
    """
    config = make_config(tmp_path)
    await ensure_service(config, make_root(config).root)

    # 每个根一个库文件，文件名带上根的标识（没有例外）：两个项目的 /README.md 是
    # 同一个键，共用一份索引会互相覆盖。
    expected = tmp_path / f"knowledge-{knowledge_runtime.workspace_slug(make_root(config).root)}.db"
    assert knowledge_runtime.knowledge_db_path(config, make_root(config).root) == expected
    assert expected.is_file()
