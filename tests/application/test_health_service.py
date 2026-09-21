"""健康检查与运行指标服务的回归测试。

覆盖面：就绪探测的通过路径与两条降级路径（数据库断开、默认模型配置不可用）、
模型目录缺失时的跳过语义、指标快照的四个计数与审计采集失败降级、构造校验。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from application.dto import CheckResult
from application.health import DATABASE_CHECK, MODEL_CHECK, HealthService
from application.run_service import RunService
from tests.application.test_run_counters import InterruptingGraph
from tests.application.test_run_service import (
    FakeGraph,
    FakeGraphFactory,
    _drain,
)
from tests.conftest import StubSessionRegistry, make_config


@dataclass(frozen=True)
class FakeProbe:
    """模型配置探测替身。

    WHY 不用真实的 ``ModelConfigProbe``：本文件只验证「服务如何把探测结果
    翻译成检查项」，与探测本身的实现无关；用替身可以避免为了让测试跑起来
    而去设置真实的 API Key 环境变量。
    """

    ok: bool
    detail: str = ""


class FakeCatalog:
    """记录调用的目录替身。"""

    def __init__(self, probe: FakeProbe) -> None:
        self._probe = probe
        self.probed_names: list[str | None] = []

    def probe(self, name: str | None = None) -> FakeProbe:
        self.probed_names.append(name)
        return self._probe


class FailingAuditStore:
    """查询即抛错的审计存储替身。"""

    async def count_all(self) -> int:
        raise RuntimeError("审计表被锁")


class ScriptedGraph:
    """按首条用户文本决定行为的图替身。

    - 文本含 ``hang``：长时间挂起，模拟「运行中的会话」；
    - 文本含 ``approve``：产出一次 HITL 中断后结束；
    - 其余：立即结束。

    WHY 用文本分派而不是按 thread_id 分派：运行配置里的 thread_id 由服务层
    构造，测试若依赖它就是在断言实现细节；按用户文本分派读起来就是用例意图。
    """

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        text = _payload_text(payload)
        if "hang" in text:
            await asyncio.sleep(10.0)
            return
        if "approve" in text:
            # WHY 用 async for 转发而不是 yield from：``yield from`` 在异步
            # 生成器中不可用（它只能委托给同步可迭代对象）。
            async for chunk in InterruptingGraph().astream(payload, config, stream_mode):
                yield chunk


def _payload_text(payload: Any) -> str:
    """从用户输入载荷中取出首条消息文本；非消息载荷返回空串。"""
    if not isinstance(payload, dict):
        return ""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return ""
    first = messages[0]
    if isinstance(first, dict):
        return str(first.get("content") or "")
    return ""


def _make_health(
    config: Any,
    thread_store: Any,
    *,
    graph: Any | None = None,
    audit_store: Any | None = None,
    catalog: Any | None = None,
    started_at: float = 0.0,
) -> tuple[HealthService, RunService]:
    """构造健康检查服务及其共享的运行服务。

    WHY 一并返回 ``RunService``：指标的真相来源就是运行服务的登记表，
    用例需要直接在它上面制造「运行中」与「待审批」两种状态。
    """
    runs = RunService(
        config,
        thread_store=thread_store,
        graph_factory=FakeGraphFactory(graph or FakeGraph()),
        workspaces=StubSessionRegistry(config),
    )
    service = HealthService(
        config,
        thread_store=thread_store,
        run_service=runs,
        audit_store=audit_store,
        catalog=catalog,
        started_at=started_at,
    )
    return service, runs


# ------------------------------------------------------------------ 构造校验


async def test_constructor_rejects_none_deps(test_config, thread_store):
    runs = RunService(
        test_config,
        thread_store=thread_store,
        graph_factory=FakeGraphFactory(FakeGraph()),
        workspaces=StubSessionRegistry(test_config),
    )

    with pytest.raises(ValueError):
        HealthService(None, thread_store=thread_store, run_service=runs)
    with pytest.raises(ValueError):
        HealthService(test_config, thread_store=None, run_service=runs)
    with pytest.raises(ValueError):
        HealthService(test_config, thread_store=thread_store, run_service=None)


def test_uptime_is_non_negative(test_config, thread_store):
    service, _ = _make_health(test_config, thread_store, started_at=0.0)

    assert service.uptime_seconds >= 0.0


# ------------------------------------------------------------------ 就绪探测


async def test_readiness_ok_when_all_deps_healthy(test_config, thread_store):
    catalog = FakeCatalog(FakeProbe(ok=True))
    service, _ = _make_health(test_config, thread_store, catalog=catalog)

    report = await service.readiness()

    assert report.ready is True
    assert [(item.name, item.ok) for item in report.checks] == [
        (DATABASE_CHECK, True),
        (MODEL_CHECK, True),
    ]
    # 探测的是配置中的默认模型，而不是硬编码的某个别名
    assert catalog.probed_names == [test_config.default_model]


async def test_readiness_reports_database_failure(test_config, thread_store):
    service, _ = _make_health(
        test_config, thread_store, catalog=FakeCatalog(FakeProbe(ok=True))
    )

    await thread_store._conn.close()
    report = await service.readiness()

    assert report.ready is False
    database = next(item for item in report.checks if item.name == DATABASE_CHECK)
    assert database.ok is False
    assert "数据库不可访问" in database.detail
    # WHY 数据库失败不应掩盖模型项的结果：运维需要一次拿到全部不健康项
    assert next(item for item in report.checks if item.name == MODEL_CHECK).ok is True


async def test_readiness_reports_model_config_failure(test_config, thread_store):
    catalog = FakeCatalog(FakeProbe(ok=False, detail="缺少环境变量 DEEPSEEK_API_KEY"))
    service, _ = _make_health(test_config, thread_store, catalog=catalog)

    report = await service.readiness()

    assert report.ready is False
    assert CheckResult(
        name=MODEL_CHECK, ok=False, detail="缺少环境变量 DEEPSEEK_API_KEY"
    ) in report.checks


async def test_readiness_skips_model_check_without_catalog(test_config, thread_store):
    service, _ = _make_health(test_config, thread_store, catalog=None)

    report = await service.readiness()

    assert report.ready is True
    model = next(item for item in report.checks if item.name == MODEL_CHECK)
    assert model.ok is True
    assert model.detail


# ------------------------------------------------------------------ 指标


async def test_metrics_starts_at_zero(test_config, thread_store, audit_store):
    service, _ = _make_health(test_config, thread_store, audit_store=audit_store)

    snapshot = await service.metrics()

    assert snapshot.running_threads == 0
    assert snapshot.started_runs == 0
    assert snapshot.pending_hitl == 0
    assert snapshot.timed_out_runs == 0
    assert snapshot.expired_hitl == 0
    assert snapshot.audit_events == 0
    assert snapshot.uptime_seconds >= 0.0


async def test_metrics_counts_running_and_pending(test_config, thread_store, audit_store):
    service, runs = _make_health(
        test_config, thread_store, graph=ScriptedGraph(), audit_store=audit_store
    )

    inflight = await runs.stream("t1", "hang 长任务")
    await _drain(await runs.stream("t2", "approve 高危命令"))

    snapshot = await service.metrics()

    assert snapshot.running_threads == 1
    assert snapshot.started_runs == 2
    assert snapshot.pending_hitl == 1
    assert snapshot.audit_events == 0

    await _drain(inflight)
    settled = await service.metrics()
    assert settled.running_threads == 0
    assert settled.started_runs == 2

    # 治理计数：本用例没有超时也没有过期，两项都必须是 0——否则说明指标
    # 把「运行过」当成了「被取消过」。
    assert settled.timed_out_runs == 0
    assert settled.expired_hitl == 0


async def test_metrics_reports_governance_counts(tmp_path, thread_store, audit_store):
    """WHY 必须让治理计数进指标：只看「运行中会话数」时，一个超时阈值配得
    过小的实例会表现为「运行数永远是 0」，看起来比健康实例更健康。"""
    service, runs = _make_health(
        make_config(tmp_path, run_max_seconds=1, hitl_pending_ttl_seconds=1),
        thread_store,
        graph=ScriptedGraph(),
        audit_store=audit_store,
    )

    inflight = await runs.stream("t1", "hang 长任务")
    await asyncio.sleep(1.05)
    runs.mark_hitl_pending("t2")
    await asyncio.sleep(1.05)
    await runs.enforce_governance()

    snapshot = await service.metrics()
    assert snapshot.timed_out_runs == 1
    assert snapshot.expired_hitl == 1

    await _drain(inflight)


async def test_metrics_audit_events_none_when_store_missing(test_config, thread_store):
    service, _ = _make_health(test_config, thread_store, audit_store=None)

    snapshot = await service.metrics()

    assert snapshot.audit_events is None


async def test_metrics_audit_events_none_when_query_fails(test_config, thread_store):
    service, _ = _make_health(test_config, thread_store, audit_store=FailingAuditStore())

    snapshot = await service.metrics()

    # WHY 断言 None 而不是 0：0 会被解读成「审计表里一条记录都没有」，
    # None 才能表达「本次采集失败」，让运维知道该去查数据库。
    assert snapshot.audit_events is None
    assert snapshot.running_threads == 0
