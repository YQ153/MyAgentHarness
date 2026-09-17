"""运行治理的回归测试：运行超时强制取消、审批挂起 TTL、并发让位。

WHY 单独成文件：这两类动作都不是用户直接触发的，而是后台协程按时间推进
自动做出决定。它们的正确性不取决于「调用是否正确」，而取决于「判定时机是否
准确」——一套用例要同时覆盖判定条件、收口动作与并发让位，与运行主流程的
用例关注点完全不同。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from application.errors import InterruptExpiredError
from application.events import AgentEventType
from application.run_service import (
    STOP_REASON_STOPPED,
    STOP_REASON_TIMEOUT,
    RunService,
)
from tests.application.test_run_service import FakeGraph, FakeGraphFactory, SlowGraph, _drain
from tests.conftest import make_config

# WHY 阈值取整数秒：配置的 ``run_max_seconds`` / ``hitl_pending_ttl_seconds``
# 是整数秒，测试用 1 秒而不是 0.05 秒，是为了不为了测试方便去放宽生产配置的
# 类型约束——代价只是这几个用例各多等一秒。
_ONE_SECOND = 1
_AFTER_A_SECOND = 1.05


class RecordingAuditStore:
    """记录审计调用的替身，用于断言治理动作确实落库。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def log(self, **kwargs: Any) -> None:
        self.events.append(kwargs)

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("event_type") == event_type]


def _make_service(
    tmp_path,
    thread_store: Any,
    graph: Any | None = None,
    *,
    audit_store: RecordingAuditStore | None = None,
    **config_overrides: Any,
) -> RunService:
    """构造带审计替身的运行服务；``run_max_seconds`` 等阈值由用例给出。"""
    return RunService(
        make_config(tmp_path, **config_overrides),
        thread_store=thread_store,
        graph_factory=FakeGraphFactory(graph or SlowGraph(delay=10.0)),
        audit_store=audit_store,
    )


# ------------------------------------------------------------------ 运行超时


async def test_timeout_forces_stop_and_reports_reason(tmp_path, thread_store):
    """WHY 断言 DONE 的 reason 是 timeout：用户没有点过停止，界面却要说
    「本轮结束」——只有带上原因，前端才能把它与「已停止」区分开。"""
    audit = RecordingAuditStore()
    service = _make_service(tmp_path, thread_store, audit_store=audit, run_max_seconds=_ONE_SECOND)

    drain_task = asyncio.create_task(_drain(await service.stream("t1", "hang")))
    await asyncio.sleep(_AFTER_A_SECOND)
    report = await service.enforce_governance()
    events = await drain_task

    assert report.timed_out_runs == 1
    assert events[-1].event is AgentEventType.DONE
    assert events[-1].payload["reason"] == STOP_REASON_TIMEOUT
    assert service.timed_out_runs == 1

    # WHY 断言审计内容：超时是系统替用户做的决定，没有审计就无法事后解释
    # 「为什么这次运行只输出了半截」。
    timeouts = audit.of_type("run_timeout")
    assert len(timeouts) == 1
    assert timeouts[0]["actor_id"] == "system"
    assert timeouts[0]["target_id"] == "t1"
    assert timeouts[0]["details"]["max_seconds"] == _ONE_SECOND


async def test_timeout_releases_run_slot(tmp_path, thread_store):
    service = _make_service(tmp_path, thread_store, run_max_seconds=_ONE_SECOND)

    drain_task = asyncio.create_task(_drain(await service.stream("t1", "hang")))
    await asyncio.sleep(_AFTER_A_SECOND)
    await service.enforce_governance()
    await drain_task

    # 槽位释放后同一会话可以立刻再次发起；否则一次超时会永久废掉该会话。
    assert service.run_handle("t1") is None
    assert "t1" not in service.running_thread_ids()


async def test_run_within_limit_is_not_cancelled(tmp_path, thread_store):
    service = _make_service(tmp_path, thread_store, run_max_seconds=60)

    drain_task = asyncio.create_task(_drain(await service.stream("t1", "hang")))
    await asyncio.sleep(0.05)
    report = await service.enforce_governance()

    assert report.timed_out_runs == 0
    assert report.checked_runs == 1
    assert service.run_handle("t1") is not None

    await service.stop("t1")
    await drain_task


async def test_zero_limit_disables_timeout(tmp_path, thread_store):
    """WHY 覆盖 ``0``：它是「显式关闭」的开关，若被当成「立即超时」，
    所有本地联调的运行都会在启动瞬间被取消。"""
    service = _make_service(tmp_path, thread_store, run_max_seconds=0)

    drain_task = asyncio.create_task(_drain(await service.stream("t1", "hang")))
    await asyncio.sleep(0.05)
    report = await service.enforce_governance()

    assert report.timed_out_runs == 0
    await service.stop("t1")
    await drain_task


async def test_sweep_ignores_run_that_already_finished(tmp_path, thread_store):
    """WHY 覆盖二次确认：快照与置位之间隔着一次 await，若运行恰好在此时
    自然结束，就会对一个已废弃的句柄记一次「超时」，指标凭空 +1。"""
    service = _make_service(tmp_path, thread_store, run_max_seconds=_ONE_SECOND)

    gen = await service.stream("t1", "hang")
    stale_handle = service.run_handle("t1")
    assert stale_handle is not None
    await service.stop("t1")
    await _drain(gen)
    assert service.run_handle("t1") is None
    await asyncio.sleep(_AFTER_A_SECOND)

    cancelled = await service._enforce_run_timeouts({"t1": stale_handle}, _ONE_SECOND, time.monotonic())

    assert cancelled == 0
    assert service.timed_out_runs == 0


# ------------------------------------------------------------------ 停止原因


async def test_first_stop_reason_wins(tmp_path, thread_store):
    """WHY 保留首次原因：先到的那个动作才是运行终止的真实原因，
    后来者（用户补点停止 / 治理协程巡检）都不应改写它。"""
    service = _make_service(tmp_path, thread_store, run_max_seconds=60)

    gen = await service.stream("t1", "hang")
    handle = service.run_handle("t1")
    assert handle is not None

    await service.stop("t1")
    handle.request_stop(STOP_REASON_TIMEOUT)
    events = await _drain(gen)

    assert handle.stop_reason == STOP_REASON_STOPPED
    assert events[-1].payload["reason"] == STOP_REASON_STOPPED


async def test_request_stop_rejects_blank_reason(tmp_path, thread_store):
    service = _make_service(tmp_path, thread_store)
    gen = await service.stream("t1", "hang")
    handle = service.run_handle("t1")

    for blank in ("", "   ", 123):
        with pytest.raises(ValueError):
            handle.request_stop(blank)

    await service.stop("t1")
    await _drain(gen)


# ------------------------------------------------------------------ 审批 TTL


async def test_stale_hitl_is_expired_and_audited(tmp_path, thread_store):
    audit = RecordingAuditStore()
    service = _make_service(tmp_path, thread_store, audit_store=audit, hitl_pending_ttl_seconds=_ONE_SECOND)

    service.mark_hitl_pending("t1")
    await asyncio.sleep(_AFTER_A_SECOND)

    report = await service.enforce_governance()

    assert report.expired_hitl == 1
    assert report.checked_hitl == 1
    # WHY 过期即释放占位：挂起数若不回落，运维看到的「待审批」会一直增长，
    # 最终没人再相信这个指标。
    assert service.pending_hitl_thread_ids() == ()
    assert service.is_hitl_expired("t1") is True
    assert service.expired_hitl == 1

    expiries = audit.of_type("hitl_expired")
    assert len(expiries) == 1
    assert expiries[0]["target_id"] == "t1"
    assert expiries[0]["details"]["ttl_seconds"] == 1


async def test_fresh_hitl_is_not_expired(tmp_path, thread_store):
    service = _make_service(tmp_path, thread_store, hitl_pending_ttl_seconds=60)

    service.mark_hitl_pending("t1")
    report = await service.enforce_governance()

    assert report.expired_hitl == 0
    assert service.pending_hitl_thread_ids() == ("t1",)
    assert service.is_hitl_expired("t1") is False


async def test_zero_ttl_disables_expiry(tmp_path, thread_store):
    service = _make_service(tmp_path, thread_store, hitl_pending_ttl_seconds=0)

    service.mark_hitl_pending("t1")
    report = await service.enforce_governance()

    assert report.expired_hitl == 0
    assert service.pending_hitl_thread_ids() == ("t1",)


async def test_expired_approval_is_rejected_on_resume(tmp_path, thread_store):
    service = _make_service(
        tmp_path, thread_store, FakeGraph(), hitl_pending_ttl_seconds=_ONE_SECOND
    )
    await _drain(await service.stream("t1", "先发起一轮以登记会话"))

    service.mark_hitl_pending("t1")
    await asyncio.sleep(_AFTER_A_SECOND)
    await service.enforce_governance()

    with pytest.raises(InterruptExpiredError, match="t1"):
        await service.resume("t1", {"decisions": [{"type": "approve"}]})


async def test_resume_before_expiry_is_allowed(tmp_path, thread_store):
    """WHY 覆盖「刚好赶上」：TTL 只应该挡住真正超期的审批，
    不能因为巡检跑过一轮就把还在窗口内的审批误杀。"""
    service = _make_service(tmp_path, thread_store, FakeGraph(), hitl_pending_ttl_seconds=60)
    await _drain(await service.stream("t1", "先发起一轮以登记会话"))

    service.mark_hitl_pending("t1")
    await service.enforce_governance()
    events = await _drain(await service.resume("t1", {"decisions": [{"type": "approve"}]}))

    assert events[-1].event is AgentEventType.DONE
    assert service.is_hitl_expired("t1") is False


async def test_new_stream_clears_expired_marker(tmp_path, thread_store):
    """WHY 必须能重新发起：过期只是作废了那一次审批，会话本身不应被判死刑。"""
    service = _make_service(tmp_path, thread_store, hitl_pending_ttl_seconds=_ONE_SECOND)

    service.mark_hitl_pending("t1")
    await asyncio.sleep(_AFTER_A_SECOND)
    await service.enforce_governance()
    assert service.is_hitl_expired("t1") is True

    await _drain(await service.stream("t1", "重新来过"))
    assert service.is_hitl_expired("t1") is False


async def test_repeated_interrupt_does_not_reset_ttl(tmp_path, thread_store):
    """WHY 不刷新起始时刻：同一轮里中断事件可能出现多次，若每次都重置，
    只要事件足够密，挂起就永远走不完 TTL。"""
    service = _make_service(tmp_path, thread_store, hitl_pending_ttl_seconds=_ONE_SECOND)

    service.mark_hitl_pending("t1")
    await asyncio.sleep(0.6)
    service.mark_hitl_pending("t1")
    await asyncio.sleep(0.6)

    report = await service.enforce_governance()

    assert report.expired_hitl == 1
    assert service.is_hitl_expired("t1") is True


# ------------------------------------------------------------------ 并发让位


async def test_expire_yields_to_concurrent_resume(tmp_path, thread_store):
    """WHY 覆盖让位：巡检判定与用户应答只差几毫秒时，系统必须认定「用户
    赢了」——他已经批准的调用不该在事后被标记成过期。"""
    service = _make_service(tmp_path, thread_store, hitl_pending_ttl_seconds=_ONE_SECOND)

    service.mark_hitl_pending("t1")
    await asyncio.sleep(_AFTER_A_SECOND)

    # 用户先应答（清登记），随后巡检才落到这一次挂起上
    service.clear_hitl_pending("t1")
    report = await service.enforce_governance()

    assert report.expired_hitl == 0
    assert service.expired_hitl == 0
    assert service.is_hitl_expired("t1") is False


async def test_expire_hitl_pending_returns_false_when_absent(tmp_path, thread_store):
    """返回值即「是否真的作废了一次挂起」，调用方据此决定是否补写审计。"""
    service = _make_service(tmp_path, thread_store)

    assert service.expire_hitl_pending("t1") is False
    assert service.expired_hitl == 0


async def test_expire_hitl_pending_rejects_blank_id(tmp_path, thread_store):
    service = _make_service(tmp_path, thread_store)

    for blank in ("", "   ", None, 123):
        with pytest.raises(ValueError):
            service.expire_hitl_pending(blank)


async def test_governance_and_stop_do_not_double_count(tmp_path, thread_store):
    """WHY 覆盖并发：巡检与用户停止可能同时命中同一次运行，无论谁先到，
    原因只能有一个，超时计数也不能超过 1。"""
    service = _make_service(tmp_path, thread_store, run_max_seconds=_ONE_SECOND)

    drain_task = asyncio.create_task(_drain(await service.stream("t1", "hang")))
    await asyncio.sleep(_AFTER_A_SECOND)

    await asyncio.gather(service.enforce_governance(), service.stop("t1"))
    events = await drain_task

    assert events[-1].payload["reason"] in (STOP_REASON_STOPPED, STOP_REASON_TIMEOUT)
    assert service.timed_out_runs <= 1


async def test_report_counts_are_consistent(tmp_path, thread_store):
    service = _make_service(tmp_path, thread_store, run_max_seconds=60, hitl_pending_ttl_seconds=60)

    drain_task = asyncio.create_task(_drain(await service.stream("t1", "hang")))
    await asyncio.sleep(0.02)
    service.mark_hitl_pending("t2")

    report = await service.enforce_governance()
    assert (report.checked_runs, report.checked_hitl) == (1, 1)
    assert (report.timed_out_runs, report.expired_hitl) == (0, 0)

    await service.stop("t1")
    await drain_task


# ------------------------------------------------------------------ 挂起时长


async def test_hitl_pending_age_tracks_waiting_time(tmp_path, thread_store):
    service = _make_service(tmp_path, thread_store)

    assert service.hitl_pending_age("t1") is None
    service.mark_hitl_pending("t1")
    await asyncio.sleep(0.05)
    age = service.hitl_pending_age("t1")

    assert age is not None and age >= 0.05
    service.clear_hitl_pending("t1")
    assert service.hitl_pending_age("t1") is None
