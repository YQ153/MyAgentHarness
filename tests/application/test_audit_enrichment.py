"""业务审计事件补齐 IP / UA 的回归测试。

覆盖面：会话删除与运行两类业务事件是否带上请求上下文、无上下文（CLI）
时是否落 NULL、审计存储故障是否影响业务结果。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from application.audit_context import request_context
from application.run_service import RunService
from application.thread_service import ThreadService


# ------------------------------------------------------------------ 测试替身


class RecordingAuditStore:
    """记录所有审计调用的存储替身。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[dict[str, Any]] = []
        self._fail = fail

    async def log(self, **kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("审计库不可用")
        self.events.append(kwargs)

    def last(self, event_type: str) -> dict[str, Any]:
        matched = [item for item in self.events if item["event_type"] == event_type]
        assert matched, f"没有记录 {event_type} 事件：{self.events}"
        return matched[-1]


class FakeGraph:
    """立即结束的图替身。"""

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[Any]:
        return
        yield  # noqa: WPS328 不可达，仅为构造异步生成器


class FakeGraphFactory:
    def get(self, name: str | None = None) -> Any:
        return FakeGraph()


class SlowGraph:
    """挂起的图替身：模拟长任务，用于停止路径的审计测试。"""

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[Any]:
        await asyncio.sleep(10.0)
        return
        yield  # noqa: WPS328 不可达，仅为构造异步生成器


class SlowGraphFactory:
    def get(self, name: str | None = None) -> Any:
        return SlowGraph()


class FakeCheckpointer:
    """不支持删除的检查点替身，用于验证删除结果分类不受审计影响。"""

    pass


async def _drain(events: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in events]


def _thread_service(config: Any, thread_store: Any, audit: RecordingAuditStore) -> ThreadService:
    return ThreadService(
        config,
        checkpointer=FakeCheckpointer(),
        thread_store=thread_store,
        graph_factory=FakeGraphFactory(),
        audit_store=audit,
    )


def _run_service(config: Any, thread_store: Any, audit: RecordingAuditStore) -> RunService:
    return RunService(
        config,
        thread_store=thread_store,
        graph_factory=FakeGraphFactory(),
        audit_store=audit,
    )


# ------------------------------------------------------------------ 用例


async def test_thread_delete_audit_carries_client_info(test_config, thread_store, tmp_path):
    audit = RecordingAuditStore()
    service = _thread_service(test_config, thread_store, audit)
    await thread_store.record_turn("t1", title_hint="hi", turn_delta=1, owner_id="alice")

    with request_context(ip="203.0.113.9", user_agent="Mozilla/5.0"):
        await service.delete_thread("t1")

    event = audit.last("thread_delete")
    assert event["ip"] == "203.0.113.9"
    assert event["user_agent"] == "Mozilla/5.0"
    assert event["actor_id"] == "anonymous"  # disabled 模式下的主体标识


async def test_run_audit_carries_client_info(test_config, thread_store):
    audit = RecordingAuditStore()
    service = _run_service(test_config, thread_store, audit)

    with request_context(ip="198.51.100.7", user_agent="harness-cli/1.0"):
        events = [event async for event in await service.stream("t1", "hello")]

    assert events[-1].event.value == "done"
    event = audit.last("thread_run")
    assert event["ip"] == "198.51.100.7"
    assert event["user_agent"] == "harness-cli/1.0"
    assert event["target_id"] == "t1"


async def test_audit_without_context_writes_null(test_config, thread_store):
    """WHY 覆盖 CLI 形态：没有中间件绑定上下文时，审计仍要落库，
    只是来源为 NULL——不能因为取不到 IP 就让整轮运行失败。"""
    audit = RecordingAuditStore()
    service = _run_service(test_config, thread_store, audit)

    await _drain(await service.stream("t1", "hello"))

    event = audit.last("thread_run")
    assert event["ip"] is None
    assert event["user_agent"] is None


async def test_audit_failure_does_not_fail_business(test_config, thread_store):
    audit = RecordingAuditStore(fail=True)
    service = _thread_service(test_config, thread_store, audit)
    await thread_store.record_turn("t1", title_hint="hi", turn_delta=1, owner_id="alice")

    with request_context(ip="203.0.113.9", user_agent="ua"):
        result = await service.delete_thread("t1")

    # 审计挂了也要把会话删掉：审计是旁路职责
    assert result.thread_id == "t1"
    assert await thread_store.get("t1") is None


async def test_stop_audit_carries_client_info(test_config, thread_store):
    audit = RecordingAuditStore()
    service = RunService(
        test_config,
        thread_store=thread_store,
        graph_factory=SlowGraphFactory(),
        audit_store=audit,
    )

    generator = await service.stream("t1", "long task")
    consumer = asyncio.create_task(_drain(generator))
    # 等生成器真正进入图执行（挂起在 SlowGraph 的 sleep 上）
    await asyncio.sleep(0.05)

    with request_context(ip="203.0.113.4", user_agent="ua-stop"):
        result = await service.stop("t1")

    assert result["reason"] == "requested"
    event = audit.last("run_cancelled")
    assert event["ip"] == "203.0.113.4"
    assert event["user_agent"] == "ua-stop"
    assert event["details"]["elapsed_seconds"] >= 0

    # 收尾：消费完生成器，避免留下悬挂任务
    events = await consumer
    assert events[-1].payload.get("reason") == "stopped"
