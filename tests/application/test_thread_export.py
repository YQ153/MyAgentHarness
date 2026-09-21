"""会话导出 / 导入的回归测试。

覆盖面：导出-导入往返内容一致、导入永远新建会话、不支持的版本被拒、工具消息靠
``tool_call_id`` 还原、缺 id 的工具消息被计数而不是静默丢弃、Markdown 可读、
导出走归属校验。

WHY 用真实检查点：导入的核心动作是「把消息写进一个新会话的检查点」，替身图证明不了
这件事——而它恰恰是这一步唯一可能真出错的地方。图本身用最小图，因为这里验的是状态
写入而不是 Agent 行为。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from application.dto import (
    EXPORT_VERSION,
    HistoryMessage,
    ThreadExport,
)
from application.errors import NotFoundError, OwnershipError
from application.thread_export import render_markdown
from application.thread_service import ThreadService
from runtime.checkpointer import checkpointer_context
from runtime.thread_store import open_thread_store
from tests.application.test_run_service import _principal


class _GraphState(TypedDict):
    messages: Annotated[list[Any], add_messages]


class _GraphFactory:
    """返回同一张图的工厂替身：本文件只需要 ``get()``。"""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def get(self, name: str | None = None, *, scope: Any = None) -> Any:
        return self._graph


def _build_graph(saver: AsyncSqliteSaver) -> Any:
    builder = StateGraph(_GraphState)

    def _noop(state: _GraphState) -> dict[str, Any]:
        return {}

    builder.add_node("noop", _noop)
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    return builder.compile(checkpointer=saver)


@asynccontextmanager
async def _service(tmp_path: Any, **config_overrides: Any):
    """构造带真实检查点与会话存储的 ThreadService。"""
    from tests.conftest import StubSessionRegistry, make_config

    overrides = dict(config_overrides)
    if overrides.get("auth_mode") not in (None, "disabled"):
        overrides.setdefault("auth_session_secret", "测试用会话密钥" * 8)
    config = make_config(tmp_path, **overrides)

    async with (
        checkpointer_context(tmp_path / "cp.db") as saver,
        open_thread_store(tmp_path / "threads.db") as store,
    ):
        yield (
            ThreadService(
                config,
                checkpointer=saver,
                thread_store=store,
                graph_factory=_GraphFactory(_build_graph(saver)),
                workspaces=StubSessionRegistry(config),
            ),
            store,
            saver,
        )


async def _seed(store: Any, saver: AsyncSqliteSaver, messages: list[Any]) -> None:
    """把一个会话及其消息写进库与检查点，作为导出源。"""
    await store.create("source", title="来源会话")
    await store.set_tags("source", ["工作"])
    graph = _build_graph(saver)
    await graph.aupdate_state(
        {"configurable": {"thread_id": "source"}}, {"messages": messages}
    )


_CONVERSATION = [
    HumanMessage(content="帮我看看这个文件"),
    AIMessage(
        content="我先读一下。",
        tool_calls=[{"id": "call-1", "name": "read_file", "args": {"file_path": "/a.md"}}],
    ),
    ToolMessage(content="文件内容", tool_call_id="call-1", name="read_file"),
    AIMessage(content="读完了，内容是这样。"),
]


# ------------------------------------------------------------------ 往返


async def test_round_trip_preserves_messages(tmp_path):
    """导出 → 导入 → 再导出，两次的消息必须逐条一致。"""
    async with _service(tmp_path) as (service, store, saver):
        await _seed(store, saver, _CONVERSATION)

        exported = await service.export_thread("source")
        result = await service.import_thread(exported)
        re_exported = await service.export_thread(result.thread_id)

        assert [message.model_dump() for message in re_exported.messages] == [
            message.model_dump() for message in exported.messages
        ]
        assert result.message_count == len(_CONVERSATION)
        assert result.skipped_messages == 0


async def test_export_records_the_source_workspace(tmp_path):
    """导出文件里记下来源工作区，并说明它不随迁移。

    WHY 必须记：消息正文里的工具输出引用是**工作区内**的虚拟路径（``/_tool_outputs/...``）。
    不记来源，导入方看到这些引用时无从判断它们指向哪棵树——而按默认工作区解释的结果可能是
    「404」，也可能是「另一个项目里的同名文件」，两种都不会说明自己少了什么。
    """
    source_root = tmp_path / "project-a"
    source_root.mkdir()
    async with _service(tmp_path) as (service, store, saver):
        await _seed(store, saver, _CONVERSATION)
        await store.record_turn("source", workspace=str(source_root))

        exported = await service.export_thread("source")

        assert exported.workspace == str(source_root.resolve())
        assert any("工作区" in note for note in exported.notes)


async def test_import_binds_the_requested_workspace_not_the_exported_one(tmp_path):
    """导入后的工作区由**导入方**决定；文件里那个只作线索。

    WHY 不照搬文件里的取值：它是来源机器上的绝对路径，在本机通常不存在，也多半不在允许
    清单里——照搬只会让导入失败，或者把新会话绑到一个清单之外的目录上（而那正是清单要
    拦的那件事）。不指定时落到启动默认值。
    """
    source_root = tmp_path / "project-a"
    target_root = tmp_path / "project-b"
    source_root.mkdir()
    target_root.mkdir()
    async with _service(tmp_path) as (service, store, saver):
        await _seed(store, saver, _CONVERSATION)
        await store.record_turn("source", workspace=str(source_root))
        exported = await service.export_thread("source")

        moved = await service.import_thread(exported, workspace=str(target_root))
        defaulted = await service.import_thread(exported)

        assert exported.workspace == str(source_root.resolve())
        assert (await store.get(moved.thread_id))["workspace"] == str(target_root.resolve())
        # 不给工作空间 → 这条新会话落在**它自己的专属目录**里（会话目录的父目录由
        # ``DB_PATH`` 派生，即 tmp_path/sessions）。这是新模型里「不绑定工作空间」那条路。
        assert (await store.get(defaulted.thread_id))["workspace"] == str(
            tmp_path / "sessions" / defaulted.thread_id
        )


async def test_import_creates_a_new_thread_and_keeps_source(tmp_path):
    """导入永远新建：来源会话不受影响，且新会话归属导入者。"""
    async with _service(tmp_path) as (service, store, saver):
        await _seed(store, saver, _CONVERSATION)

        exported = await service.export_thread("source")
        result = await service.import_thread(exported)

        assert result.thread_id != "source"
        assert await store.get("source") is not None
        # 标签随文件带过去，来源会话仍在
        assert (await store.get(result.thread_id))["tags"] == ["工作"]


async def test_import_counts_turns_for_the_list(tmp_path):
    """清单上的轮数必须与内容相符，否则用户会以为这个会话是空的。"""
    async with _service(tmp_path) as (service, store, saver):
        await _seed(store, saver, _CONVERSATION)

        result = await service.import_thread(await service.export_thread("source"))

        record = await store.get(result.thread_id)
        assert record is not None
        assert record["turn_count"] == 1  # 一条用户消息


async def test_import_rejects_unknown_version(tmp_path):
    async with _service(tmp_path) as (service, _store, _saver):
        payload = ThreadExport(version="99", thread_id="x")

        with pytest.raises(ValueError, match="版本"):
            await service.import_thread(payload)


# ------------------------------------------------------------------ 消息还原


async def test_import_keeps_tool_messages(tmp_path):
    """工具消息必须靠 ``tool_call_id`` 还原，而不是被丢掉。"""
    async with _service(tmp_path) as (service, store, saver):
        await _seed(store, saver, _CONVERSATION)

        result = await service.import_thread(await service.export_thread("source"))
        restored = await service.history(result.thread_id)

        roles = [message.role for message in restored]
        assert "tool" in roles
        tool_message = next(message for message in restored if message.role == "tool")
        assert tool_message.tool_call_id == "call-1"
        assert tool_message.name == "read_file"


async def test_missing_tool_call_id_is_counted_not_silently_dropped(tmp_path):
    """缺 id 的工具消息要报数，不能悄悄少一条。"""
    async with _service(tmp_path) as (service, _store, _saver):
        payload = ThreadExport(
            version=EXPORT_VERSION,
            thread_id="x",
            messages=[
                HistoryMessage(role="human", content="问"),
                HistoryMessage(role="tool", content="无主的工具结果"),  # 没有 tool_call_id
                HistoryMessage(role="system", content="未知角色"),  # 无法还原
            ],
        )

        result = await service.import_thread(payload)

        assert result.message_count == 1
        assert result.skipped_messages == 2


# ------------------------------------------------------------------ 渲染与校验


async def test_markdown_has_roles_and_content(tmp_path):
    async with _service(tmp_path) as (service, store, saver):
        await _seed(store, saver, _CONVERSATION)

        text = render_markdown(await service.export_thread("source"))

        assert "# 来源会话" in text
        assert "## 用户" in text and "## 助手" in text and "## 工具" in text
        assert "帮我看看这个文件" in text
        assert "`read_file`" in text
        assert "- 标签：工作" in text


async def test_export_requires_ownership(tmp_path):
    """别人的会话不能被导出——导出文件里是完整正文。"""
    async with _service(tmp_path, auth_mode="apikey") as (service, store, saver):
        await store.create("source", title="来源会话", owner_id="alice")
        await store.set_tags("source", ["工作"])
        await _seed(store, saver, _CONVERSATION)

        with pytest.raises(OwnershipError):
            await service.export_thread("source", _principal("bob"))


async def test_export_unknown_thread_is_not_found(tmp_path):
    async with _service(tmp_path) as (service, _store, _saver):
        with pytest.raises(NotFoundError):
            await service.export_thread("never-existed")
