"""长期记忆的端到端测试：真实图 + 真实 SQLite Store + 脚本化模型。

WHY 需要端到端而不是只测服务层：记忆的隔离发生在「模型调用工具 → backend →
Store 命名空间」这条链路上，其中任何一环（图的 context_schema、命名空间工厂、
异步接口选择）接错，服务层用例都不会发现。这里用脚本化模型驱动真实的
``create_deep_agent``，既覆盖了工具执行路径，也顺带钉住「deepagents 用异步
接口访问 Store」这一前提——一旦它改成同步调用，``AsyncSqliteStore`` 会在
事件循环里直接抛 ``InvalidStateError``，本用例会立刻红。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from deepagents import create_deep_agent
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.store.base import BaseStore

from agent.backends import build_backend
from agent.run_context import AgentRunContext, memory_namespace
from runtime.store import open_store
from tests.conftest import make_config


class ScriptedChatModel(BaseChatModel):
    """按脚本逐轮产出 ``AIMessage`` 的假模型。

    WHY 不用 ``GenericFakeChatModel``：deepagents 装配时会调用 ``bind_tools``，
    而通用假模型未必实现它；这里显式返回自身，把「模型行为」压缩到只有
    「第几轮返回哪条消息」这一件事。
    """

    replies: list[BaseMessage]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # 按「已经出现过几条 AI 消息」推进脚本：最后一条会被重复使用，
        # 这样用例只关心有几步，不必为每个收尾场景补一条消息。
        index = sum(1 for message in messages if isinstance(message, AIMessage))
        reply = self.replies[min(index, len(self.replies) - 1)]
        return ChatResult(generations=[ChatGeneration(message=reply)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)


def _tool_call(name: str, args: dict[str, Any], call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _config(tmp_path: Path):
    """构造配置并建好工作区目录（``build_backend`` 要求工作区存在）。"""
    config = make_config(tmp_path)
    config.ensure_directories()
    return config


async def _run_agent(
    config,
    store: BaseStore,
    replies: list[BaseMessage],
    *,
    user_id: str,
    thread_id: str = "t1",
) -> list[Any]:
    """装配并运行一次图，返回全部流分片。"""
    agent = create_deep_agent(
        model=ScriptedChatModel(replies=replies),
        backend=build_backend(config, store),
        store=store,
        context_schema=AgentRunContext,
        name="memory-e2e",
    )
    return [
        chunk
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": "记住我的偏好"}]},
            config={"configurable": {"thread_id": thread_id}, "recursion_limit": 50},
            stream_mode=["messages", "updates"],
            context=AgentRunContext(user_id=user_id),
        )
    ]


def _tool_outputs(chunks: list[Any]) -> list[str]:
    """从流分片里取出所有工具结果文本。"""
    outputs: list[str] = []
    for mode, payload in chunks:
        if mode != "messages":
            continue
        message, _meta = payload
        if type(message).__name__ == "ToolMessage":
            outputs.append(str(message.content))
    return outputs


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[BaseStore]:
    async with open_store(tmp_path / "agent.db") as opened:
        yield opened


async def test_agent_writes_memory_into_owner_namespace(tmp_path: Path, store: BaseStore):
    """Agent 写 ``/memories/`` 必须落到本轮主体的命名空间里。"""
    config = _config(tmp_path)

    await _run_agent(
        config,
        store,
        [
            _tool_call("write_file", {"file_path": "/memories/notes.md", "content": "用户偏好中文"}),
            AIMessage(content="已记住。"),
        ],
        user_id="alice",
    )

    alice_items = await store.asearch(memory_namespace("alice"))
    assert [item.key for item in alice_items] == ["/notes.md"]
    assert alice_items[0].value["content"] == "用户偏好中文"
    assert await store.asearch(memory_namespace("bob")) == []


async def test_agent_cannot_read_another_users_memory(tmp_path: Path, store: BaseStore):
    """跨用户读取必须落空——这是「记忆按主体隔离」的最终验收线。"""
    config = _config(tmp_path)
    await store.aput(
        memory_namespace("bob"),
        "/secret.md",
        {"content": "bob 的私有偏好", "encoding": "utf-8"},
    )

    chunks = await _run_agent(
        config,
        store,
        [
            _tool_call("read_file", {"file_path": "/memories/secret.md"}),
            AIMessage(content="读不到。"),
        ],
        user_id="alice",
    )

    assert "bob 的私有偏好" not in "\n".join(_tool_outputs(chunks))


async def test_agent_reads_back_its_own_memory(tmp_path: Path, store: BaseStore):
    """同一主体读自己写的记忆必须成功——否则「隔离」会退化成「全都读不到」。"""
    config = _config(tmp_path)
    await store.aput(
        memory_namespace("alice"),
        "/secret.md",
        {"content": "alice 的私有偏好", "encoding": "utf-8"},
    )

    chunks = await _run_agent(
        config,
        store,
        [
            _tool_call("read_file", {"file_path": "/memories/secret.md"}),
            AIMessage(content="读到了。"),
        ],
        user_id="alice",
    )

    assert "alice 的私有偏好" in "\n".join(_tool_outputs(chunks))
