"""RunService 的并发与入口语义回归测试。

WHY 用假图而不是真实 LangGraph 图：这些测试只关心「槽位互斥与释放、
会话校验与记忆归属」的应用层语义；真图会引入模型初始化与检查点依赖，
让测试变慢且受 API Key 牵制。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from application.errors import (
    NotFoundError,
    ThreadBusyError,
)
from application.events import AgentEventType
from agent.run_context import ANONYMOUS_USER_ID
from application.run_service import RunHandle, RunService
from runtime.thread_store import ThreadMetaStore

from tests.conftest import StubSessionRegistry


# ------------------------------------------------------------------ 测试替身


class FakeGraph:
    """最小图替身：astream 不产出任何 chunk，一轮运行立即结束。"""

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[Any]:
        # WHY「return + 不可达 yield」：yield 的唯一作用是把本方法标记为
        # 异步生成器，调用方的 `async for` 语法才能成立。
        return
        yield  # noqa: WPS328 不可达，仅为构造异步生成器


class SlowGraph:
    """挂起的图替身：模拟长任务，用于取消语义测试。"""

    def __init__(self, delay: float = 10.0) -> None:
        self._delay = delay

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[Any]:
        await asyncio.sleep(self._delay)
        return
        yield  # noqa: WPS328 不可达，仅为构造异步生成器


class FakeGraphFactory:
    """图工厂替身：模仿 ModelRegistry 的「未知别名抛 KeyError」契约。"""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def get(self, name: str | None = None, *, scope: Any = None) -> Any:
        if name is not None and name != "deepseek-flash":
            raise KeyError(name)
        return self._graph


def _make_service(
    config: Any,
    store: ThreadMetaStore,
    graph: Any | None = None,
) -> RunService:
    return RunService(
        config,
        thread_store=store,
        graph_factory=FakeGraphFactory(graph or FakeGraph()),
        workspaces=StubSessionRegistry(config),
    )


async def _drain(events: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in events]


# ------------------------------------------------------------------ 构造校验


async def test_constructor_rejects_none_deps(test_config, thread_store):
    """每一项必需依赖为 ``None`` 都要当场失败。

    WHY 把 ``workspaces`` 也列进来：它是「本轮跑在哪个工作区」的唯一来源，缺了它
    就必须要么报错、要么悄悄退回某个默认值——后者正是本次要消除的那类失败。
    """
    workspaces = StubSessionRegistry(test_config)
    graph = FakeGraphFactory(FakeGraph())
    with pytest.raises(ValueError):
        RunService(None, thread_store=thread_store, graph_factory=graph, workspaces=workspaces)
    with pytest.raises(ValueError):
        RunService(test_config, thread_store=None, graph_factory=graph, workspaces=workspaces)
    with pytest.raises(ValueError):
        RunService(test_config, thread_store=thread_store, graph_factory=None, workspaces=workspaces)
    with pytest.raises(ValueError):
        RunService(test_config, thread_store=thread_store, graph_factory=graph, workspaces=None)


# ------------------------------------------------------------------ 输入校验


async def test_stream_rejects_blank_input(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    for blank in ("", "   ", None, 123):
        with pytest.raises(ValueError):
            await service.stream("t1", blank)


async def test_stream_rejects_unknown_model(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    with pytest.raises(KeyError):
        await service.stream("t1", "hello", model_name="gpt-unknown")


# ------------------------------------------------------------------ 运行与槽位


async def test_stream_yields_done_and_releases_slot(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    events = await _drain(await service.stream("t1", "hello world"))

    assert events[-1].event == AgentEventType.DONE
    assert events[-1].payload["thread_id"] == "t1"

    # 槽位已释放：同会话可立即再次发起
    again = await _drain(await service.stream("t1", "next round"))
    assert again[-1].event == AgentEventType.DONE


async def test_busy_slot_rejects_concurrent_run(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    # 拿到生成器但尚未消费：槽位已被占用（占用发生在 stream 返回前）
    pending = await service.stream("t1", "slow one")

    with pytest.raises(ThreadBusyError) as exc_info:
        await service.stream("t1", "second try")
    assert exc_info.value.thread_id == "t1"

    # 互斥是 per-thread 的：其他会话不受影响
    other = await service.stream("t2", "other thread")
    await _drain(other)

    # 消费完第一个生成器后槽位释放
    await _drain(pending)
    resumed = await service.stream("t1", "third try")
    await _drain(resumed)


async def test_cancel_mid_run_releases_slot(test_config, thread_store):
    """WHY 本测试是 stop 功能（T2）的前置契约：取消必须释放槽位，
    否则会话会被永久判定为运行中。"""
    service = _make_service(test_config, thread_store, SlowGraph(delay=10.0))

    generator = await service.stream("t1", "long task")
    task = asyncio.create_task(generator.__anext__())
    # 给生成器一点启动时间，让它真正进入图执行（挂起在 SlowGraph 的 sleep 上）
    await asyncio.sleep(0.05)

    with pytest.raises(ThreadBusyError):
        await service.stream("t1", "during run")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 取消后槽位必须已释放，且会话可重新发起并正常完成
    events = await _drain(await service.stream("t1", "after cancel"))
    assert events[-1].event == AgentEventType.DONE


async def test_stream_records_turn_metadata(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    await _drain(await service.stream("t1", "hello world"))

    record = await thread_store.get("t1")
    assert record is not None
    assert record["turn_count"] == 1
    assert record["title"] == "hello world"
    # disabled 模式下所有者为空串
    assert record["owner_id"] == ""


# ------------------------------------------------------------------ 记忆归属


class ContextRecordingGraph:
    """记录 ``astream`` 收到的 context，用于断言记忆归属确实进了图。"""

    def __init__(self) -> None:
        self.contexts: list[Any] = []

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[Any]:
        self.contexts.append(context)
        return
        yield  # noqa: WPS328 不可达，仅为构造异步生成器


async def test_stream_passes_memory_owner_into_graph_context(test_config, thread_store):
    """归属必须随每轮运行进图：命名空间在图内算，缺了它记忆会落进另一个池子。

    WHY 断言与会话侧的兜底是同一个标识：两处一旦漂移，会得到「面板说没记住、
    Agent 却照着做」这种现象——功能没坏，但两边看到的是两份事实。
    """
    graph = ContextRecordingGraph()
    service = _make_service(test_config, thread_store, graph)

    await _drain(await service.stream("t1", "hello"))

    assert graph.contexts[0].user_id == ANONYMOUS_USER_ID


async def test_resume_passes_memory_owner_into_graph_context(test_config, thread_store):
    """恢复执行同样是「一轮运行」，认证关闭时归属必须与发起时同一口径。"""
    graph = ContextRecordingGraph()
    service = _make_service(test_config, thread_store, graph)
    await _drain(await service.stream("t1", "hello"))

    await _drain(await service.resume("t1", {"decisions": [{"type": "approve"}]}))

    assert graph.contexts[-1].user_id == ANONYMOUS_USER_ID


def test_memory_owner_falls_back_to_anonymous_for_empty_owner():
    """``owner_id`` 为空串时必须归一到匿名标识——否则 CLI 与 Web 各写一个命名空间。"""
    handle = RunHandle(
        thread_id="t1",
        started_at=0.0,
        cancel_event=asyncio.Event(),
        owner_id="",
    )

    assert handle.memory_owner == ANONYMOUS_USER_ID


def test_memory_owner_prefers_owner_id():
    handle = RunHandle(
        thread_id="t1",
        started_at=0.0,
        cancel_event=asyncio.Event(),
        owner_id="alice",
    )

    assert handle.memory_owner == "alice"


# ------------------------------------------------------------------ 恢复运行


async def test_resume_completes_for_known_thread(test_config, thread_store):
    """恢复路径的正面覆盖：会话存在时，一轮恢复必须以 DONE 收尾。"""
    service = _make_service(test_config, thread_store)
    await _drain(await service.stream("t1", "hello"))

    events = await _drain(
        await service.resume("t1", {"decisions": [{"type": "approve"}]})
    )

    assert events[-1].event == AgentEventType.DONE


async def test_resume_rejects_unknown_thread(test_config, thread_store):
    """会话不存在时恢复必须报「会话不存在」，而不是凭空造一个会话出来。"""
    service = _make_service(test_config, thread_store)

    with pytest.raises(NotFoundError):
        await service.resume("missing", {"decisions": [{"type": "approve"}]})
