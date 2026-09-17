"""运行计数器与 HITL 挂起登记的回归测试。

WHY 单独成文件：``test_run_service.py`` 关注的是「槽位互斥与权限」，
而这里关注的是「运行治理指标的数据来源」——两者共用测试替身，
但断言的语义不同，混在一起会让任一方的失败定位变慢。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from application.errors import ThreadBusyError
from application.events import AgentEventType
from tests.application.test_run_service import (
    SlowGraph,
    _drain,
    _make_service,
)


class InterruptingGraph:
    """产出一次 HITL 中断后立即结束的图替身。"""

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        yield ("updates", {"__interrupt__": (_FakeInterrupt(),)})


class InterruptOnceGraph:
    """首轮中断、之后正常结束的图替身。

    用于验证「用户不审批、直接发起新一轮」时挂起登记会被清除。
    """

    def __init__(self) -> None:
        self.calls = 0

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        self.calls += 1
        if self.calls == 1:
            yield ("updates", {"__interrupt__": (_FakeInterrupt(),)})


class _FakeInterrupt:
    """模仿 LangGraph ``Interrupt`` 的最小结构。"""

    id = "interrupt-1"
    value = {
        "action_requests": [
            {"name": "execute", "args": {"command": "rm -rf /"}, "description": "高危命令"}
        ],
        "review_configs": [
            {"action_name": "execute", "allowed_decisions": ["approve", "reject"]}
        ],
    }


# ------------------------------------------------------------------ 累计运行数


async def test_started_runs_counts_stream_and_resume(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    await _drain(await service.stream("t1", "第一轮"))
    assert service.started_runs == 1

    await _drain(await service.resume("t1", {"decisions": [{"type": "approve"}]}))
    assert service.started_runs == 2


async def test_busy_attempt_does_not_inflate_counter(test_config, thread_store):
    """WHY 本用例是计数口径契约：被 ThreadBusyError 拒绝的尝试没有真正
    发起运行，计入会让「累计运行数」高于实际发生过的运行次数。"""
    service = _make_service(test_config, thread_store, SlowGraph(delay=10.0))
    pending = await service.stream("t1", "占用中")

    with pytest.raises(ThreadBusyError):
        await service.stream("t1", "并发的第二轮")

    assert service.started_runs == 1
    await _drain(pending)


# ------------------------------------------------------------------ HITL 挂起


async def test_interrupt_marks_thread_pending(test_config, thread_store):
    service = _make_service(test_config, thread_store, InterruptingGraph())

    events = await _drain(await service.stream("t1", "执行高危命令"))

    assert [event.event for event in events][0] is AgentEventType.INTERRUPT
    assert service.pending_hitl_thread_ids() == ("t1",)


async def test_resume_clears_pending(test_config, thread_store):
    # WHY 用「首轮中断、之后正常结束」的图：若图每次都中断，恢复后又会立刻
    # 登记一条新的挂起，就断言不出「恢复清除了旧挂起」这一步。
    service = _make_service(test_config, thread_store, InterruptOnceGraph())
    await _drain(await service.stream("t1", "先中断"))
    assert service.pending_hitl_thread_ids() == ("t1",)

    await _drain(await service.resume("t1", {"decisions": [{"type": "approve"}]}))

    assert service.pending_hitl_thread_ids() == ()


async def test_new_stream_supersedes_pending(test_config, thread_store):
    """WHY 覆盖「用户不审批、直接改口」：此时旧的审批卡已不代表当前意图，
    留着登记会让待审批数只增不减。"""
    service = _make_service(test_config, thread_store, InterruptOnceGraph())

    await _drain(await service.stream("t1", "先中断"))
    assert service.pending_hitl_thread_ids() == ("t1",)

    await _drain(await service.stream("t1", "换个说法"))
    assert service.pending_hitl_thread_ids() == ()


async def test_clear_hitl_pending_is_idempotent(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    service.clear_hitl_pending("t1")
    service.clear_hitl_pending("t1")

    assert service.pending_hitl_thread_ids() == ()


async def test_hitl_marking_validates_thread_id(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    for invalid in ("", "   ", None, 123):
        with pytest.raises(ValueError):
            service.mark_hitl_pending(invalid)
        with pytest.raises(ValueError):
            service.clear_hitl_pending(invalid)
