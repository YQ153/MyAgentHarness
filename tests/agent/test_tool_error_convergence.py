"""工具异常收敛的契约：失败回传模型，但控制流必须原样穿透。

WHY 必须有这组用例：这一层的两种失效方式都不报错，只静默改变行为——

1. **该收敛的没收敛**：一次抓取失败就终结整轮对话，用户只看到一句「运行失败」，而模型
   连改参数的机会都没有（2026-09-23 的 DNS 污染事故正是如此）。
2. **不该收敛的收敛了**：HITL 审批靠 ``GraphBubbleUp`` 冒泡，被吞掉后审批会退化成一次
   「工具失败」而流程继续——审批链静默失效。

覆盖分两段：前半直接驱动两个钩子，把「异常进去 → 什么出来」逐条钉死（穿透、截断这些在
真实图里不好构造）；后半用脚本化模型驱动**真实图**——一条走生产装配路径（``build_agent``）
证明事故场景不再复现，一条刻意不带本层，把事故原样重放作为反证。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import ToolCallRequest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.errors import GraphInterrupt
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from agent.backends import build_backend
from agent.run_context import AgentRunContext
from agent.tool_errors import ToolErrorConvergenceMiddleware
from config import AppConfig
from tests.conftest import make_config, make_root

_FAILED_URL = "https://zh.m.wikipedia.org/wiki/朱元璋"
"""事故现场用的地址：让「失败文案里带没带目标」这条断言读起来有据可依。"""

_Handler = Callable[[ToolCallRequest], ToolMessage | Command[Any]]
_AsyncHandler = Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]


class _ToolFailure(RuntimeError):
    """工具侧失败的替身。

    WHY 不复用 ``WebToolError``：本层对异常类型没有任何假设（控制流异常之外一律收敛），
    用例拿等价的 ``RuntimeError`` 子类即可，避免把中间件的用例绑到某个具体工具模块上。
    """


def _request(name: str = "web_fetch", *, call_id: str = "call-1") -> ToolCallRequest:
    """构造一次工具调用请求（形状与框架交给拦截器的完全一致）。"""
    return ToolCallRequest(
        tool_call={
            "name": name,
            "args": {"url": _FAILED_URL},
            "id": call_id,
            "type": "tool_call",
        },
        tool=None,
        state={},
        runtime=None,  # type: ignore[arg-type]  # 图外驱动，本层不使用运行时
    )


def _raising(exc: BaseException) -> _Handler:
    """同步处理器：直接抛出给定异常。"""

    def handler(request: ToolCallRequest) -> ToolMessage | Command[Any]:
        raise exc

    return handler


def _async_raising(exc: BaseException) -> _AsyncHandler:
    """异步处理器：直接抛出给定异常。"""

    async def handler(request: ToolCallRequest) -> ToolMessage | Command[Any]:
        raise exc

    return handler


# --------------------------------------------------------------- 收敛


async def test_async_hook_converges_tool_failure() -> None:
    """工具抛出的异常变成 ``status="error"`` 的工具结果，而不是终结整轮运行。"""
    result = await ToolErrorConvergenceMiddleware().awrap_tool_call(
        _request(), _async_raising(_ToolFailure(f"抓取被拒绝：{_FAILED_URL}"))
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call-1"
    assert result.name == "web_fetch"
    # 文案要能让模型与用户认出「哪次调用、哪类失败、目标是什么」
    assert "web_fetch" in result.content
    assert "_ToolFailure" in result.content
    assert _FAILED_URL in result.content


def test_sync_hook_converges_tool_failure() -> None:
    """同步钩子做同样的事。

    WHY 单独钉住：只实现异步钩子的中间件会被框架一并编进同步链，同步调用路径将撞上
    基类的 ``NotImplementedError``——那时的症状是「同步跑必崩」，与工具失败无关。
    """
    result = ToolErrorConvergenceMiddleware().wrap_tool_call(
        _request("search_documents", call_id="call-2"), _raising(_ToolFailure("索引不可用"))
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call-2"
    assert "search_documents" in result.content


async def test_success_result_is_passed_through() -> None:
    """成功路径不做任何重新包装。"""
    original = ToolMessage(content="正文", name="web_fetch", tool_call_id="call-1")

    async def handler(request: ToolCallRequest) -> ToolMessage | Command[Any]:
        return original

    assert await ToolErrorConvergenceMiddleware().awrap_tool_call(_request(), handler) is original


async def test_long_error_detail_is_truncated() -> None:
    """超长异常正文按上限截断：工具结果整体进上下文，不能任其膨胀。"""
    result = await ToolErrorConvergenceMiddleware().awrap_tool_call(
        _request(), _async_raising(_ToolFailure("x" * 10_000))
    )

    assert isinstance(result, ToolMessage)
    assert len(result.content) < 10_000
    assert "已截断" in result.content


# --------------------------------------------------------------- 穿透


async def test_interrupt_is_not_converged() -> None:
    """HITL 审批的中断原样穿透：被吞掉等于审批链静默失效。"""
    with pytest.raises(GraphInterrupt):
        await ToolErrorConvergenceMiddleware().awrap_tool_call(
            _request("execute"), _async_raising(GraphInterrupt())
        )


async def test_cancelled_error_is_not_converged() -> None:
    """取消语义（用户停止 / 客户端断开）原样穿透。"""
    with pytest.raises(asyncio.CancelledError):
        await ToolErrorConvergenceMiddleware().awrap_tool_call(
            _request(), _async_raising(asyncio.CancelledError())
        )


# --------------------------------------------------------------- 观测


async def test_failure_is_logged_with_stack(caplog: pytest.LogCaptureFixture) -> None:
    """收敛不等于静默吞错：必须留下带堆栈的 ERROR 日志。"""
    caplog.set_level(logging.ERROR, logger="agent.tool_errors")

    await ToolErrorConvergenceMiddleware().awrap_tool_call(
        _request(), _async_raising(_ToolFailure("炸了"))
    )

    records = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert records, "工具失败没有留下 ERROR 日志"
    assert records[0].exc_info, "ERROR 日志缺堆栈：被消费掉的异常以后将无法排查"
    assert "web_fetch" in caplog.text


# --------------------------------------------------------------- 端到端


class _ScriptedChatModel(BaseChatModel):
    """按脚本逐轮产出 ``AIMessage`` 的假模型（与 ``test_memory_end_to_end`` 同款）。

    WHY 自建而不是用 ``GenericFakeChatModel``：装配图时会调用 ``bind_tools``，通用假模型
    没有实现它（实测抛 ``NotImplementedError``）。
    """

    replies: list[BaseMessage]

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-errors"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # 按「已出现过几条 AI 消息」推进脚本：最后一条会被重复使用，收尾场景无需补条目
        index = sum(1 for message in messages if isinstance(message, AIMessage))
        return ChatResult(
            generations=[ChatGeneration(message=self.replies[min(index, len(self.replies) - 1)])]
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        # WHY 这里用异步回调管理器的类型（与同目录的假模型用例不同）：基类该形参就是
        # ``AsyncCallbackManagerForLLMRun``，写成同步类型会被类型检查判为违反 LSP。
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, None, **kwargs)


@tool("fetch_page")
async def _fetch_page(url: str) -> str:
    """抓取一个页面并返回正文。

    WHY 用真实的 ``@tool`` 而不是手工构造 ``StructuredTool``：这条用例要走的正是
    「工具节点 → 中间件 → 模型」这条生产链路，工具怎么造出来会影响它被谁执行。
    """
    raise _ToolFailure(f"抓取被出站安全策略拒绝：{url}")


def _scripted_model() -> _ScriptedChatModel:
    """两轮脚本：先调工具，拿到失败结果后自行换来源收尾。"""
    return _ScriptedChatModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "fetch_page",
                        "args": {"url": _FAILED_URL},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="这个地址抓不到，我换用其它来源。"),
        ]
    )


class _ScriptedRegistry:
    """模型注册表替身：把「用哪个模型」固定成脚本化模型。"""

    default_name = "scripted"

    def __init__(self, model: BaseChatModel) -> None:
        self._model = model

    def get(self, name: str | None = None) -> BaseChatModel:
        return self._model


async def test_tool_failure_is_returned_to_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事故回归：工具抛异常时整轮对话继续，模型拿到失败结果后自己换路子。

    WHY 走 ``build_agent`` 而不是自己拼 ``create_deep_agent``：本层是否真的挂在
    **生产那一份**中间件列表里，只有这条路径能证明。
    """
    from agent import graph as graph_module

    config: AppConfig = make_config(tmp_path)
    config.ensure_directories()
    scope = make_root(config)
    model = _scripted_model()
    monkeypatch.setattr(
        graph_module, "build_default_registry", lambda config: _ScriptedRegistry(model)
    )

    graph = graph_module.build_agent(config, scope=scope, store=InMemoryStore(), tools=(_fetch_page,))
    state = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "帮我打开这个页面"}]},
        config={"configurable": {"thread_id": "tool-errors"}},
        context=AgentRunContext(workspace=str(scope.root)),
    )

    messages = list(state["messages"])
    results = [message for message in messages if isinstance(message, ToolMessage)]
    assert [message.status for message in results] == ["error"]
    assert "_ToolFailure" in results[0].content
    assert results[0].tool_call_id == "call-1"
    # 关键：整轮没有以失败收场——模型读到失败结果后继续给出了最终答复
    assert messages[-1].content == "这个地址抓不到，我换用其它来源。"


async def test_without_the_middleware_the_failure_kills_the_run(tmp_path: Path) -> None:
    """反证：没有这一层时同一个异常会终结整轮运行（2026-09-23 事故的原样重放）。

    WHY 需要有这条：它把「这一层为什么存在」钉在行为上。否则将来有人把挂载删掉，
    中间件本身的用例仍然全绿——那些用例压根不经过图。
    """
    config: AppConfig = make_config(tmp_path)
    config.ensure_directories()
    scope = make_root(config)
    store = InMemoryStore()
    graph = create_deep_agent(
        model=_scripted_model(),
        backend=build_backend(config, store, scope=scope),
        tools=[_fetch_page],
        store=store,
        context_schema=AgentRunContext,
        name="no-convergence",
    )

    with pytest.raises(_ToolFailure):
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "帮我打开这个页面"}]},
            config={"configurable": {"thread_id": "tool-errors-baseline"}},
            context=AgentRunContext(workspace=str(scope.root)),
        )
