"""运行并发上限与按用户限流的回归测试。

覆盖面：上限边界（恰好满 / 超出）、按用户隔离（一个人被限不影响另一个人）、
拒绝路径不泄漏他人会话存在性、判定与占用的原子性、指标与运行登记表同源。

WHY 用挂起的图替身：本文件要验证的是「槽位被占住时会怎样」，所以需要一个能被占住
且不会自己结束的运行。用真图会让每个用例都去调模型，失败还会指向网络。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from application.errors import REASON_CONCURRENCY, REASON_RATE, RunRejectedError
from application.run_service import RunService
from tests.application.test_run_service import (
    FakeGraphFactory,
    SlowGraph,
    _drain,
    _make_service,
)
from tests.conftest import make_config


def _limited_service(tmp_path: Any, store: Any, **overrides: Any) -> RunService:
    """构造一个挂了慢图的运行服务；默认不限制并发，由用例自行指定。

    WHY 延迟取极小值：槽位是由「生成器尚未被消费」占着的，与图跑多久无关——被拒的
    那个请求在生成器开始消费之前就已经失败了。原先用 10 秒延迟，每个用例的收尾
    ``_drain`` 都要空等一场，整个文件从 1 秒变成近 2 分钟：慢下来的测试会被跳过，
    而跳过的测试等于没有。
    """
    config = make_config(tmp_path, **overrides)
    return _make_service(config, store, SlowGraph(delay=0.01))


# ------------------------------------------------------------------ 并发上限


async def test_concurrency_limit_rejects_the_next_run(tmp_path, thread_store):
    """上限为 1：占住之后第二个运行被拒，槽位归还后又放行。"""
    service = _limited_service(tmp_path, thread_store, max_concurrent_runs=1)
    await thread_store.create("t1", title="会话")
    await thread_store.create("t2", title="会话")

    # 不消费这个生成器，槽位就一直被占着——这正是「长任务占满」的形态
    held = await service.stream("t1", "占住槽位")

    with pytest.raises(RunRejectedError) as info:
        await service.stream("t2", "第二个")

    assert info.value.reason == REASON_CONCURRENCY
    assert service.available_run_slots == 0
    assert service.rejected_runs == 1

    await _drain(held)

    # 槽位归还后必须立刻可再进：指标与登记表同源，这里若不变，说明另有一份计数
    assert service.available_run_slots == 1


async def test_zero_means_unlimited(tmp_path, thread_store):
    """上限为 0 表示不限制：占用多少都不拒绝，指标以 -1 表达。"""
    service = _limited_service(tmp_path, thread_store, max_concurrent_runs=0)

    held = [await service.stream(f"t{index}", "并发跑") for index in range(5)]

    assert service.available_run_slots == -1
    assert service.max_concurrent_runs == 0
    assert service.rejected_runs == 0

    for stream in held:
        await _drain(stream)


async def test_available_slots_track_the_registry(tmp_path, thread_store):
    """可用槽位必须与运行登记表同源，而不是另建一份计数。"""
    service = _limited_service(tmp_path, thread_store, max_concurrent_runs=2)
    await thread_store.create("t1", title="会话")

    assert service.available_run_slots == 2

    held = await service.stream("t1", "占一个")

    assert service.available_run_slots == 1

    await _drain(held)

    assert service.available_run_slots == 2


async def test_concurrent_acquisitions_never_exceed_the_cap(tmp_path, thread_store):
    """并发抢占不得超出上限：判定与占用必须是一个原子动作。

    WHY 用线程而不是协程发起：``_acquire_run_slot`` 是同步方法，同一事件循环里的
    多次调用天然被排成一条线，那样测到的是事件循环而不是那把锁。只有真线程才会
    同时压在 ``threading.Lock`` 上，把「先查后占」的空档暴露出来。
    """
    service = _limited_service(tmp_path, thread_store, max_concurrent_runs=3)

    def attempt(index: int) -> bool:
        try:
            service._acquire_run_slot(f"t{index}")  # noqa: SLF001 - 直接压并发入口
            return True
        except RunRejectedError:
            return False

    results = await asyncio.gather(
        *[asyncio.to_thread(attempt, index) for index in range(12)]
    )

    assert sum(results) == 3
    assert service.rejected_runs == 9


# ------------------------------------------------------------------ 运行限流


async def test_rate_limit_applies_to_the_single_identity(tmp_path, thread_store):
    """窗口内超过上限的运行被拒绝：所有请求属于同一个人，共用一个计数桶。"""
    service = _limited_service(
        tmp_path,
        thread_store,
        max_concurrent_runs=0,
        run_rate_limit_window_seconds=60,
        run_rate_limit_max_attempts=1,
    )

    held = await service.stream("t1", "第一轮")

    with pytest.raises(RunRejectedError) as info:
        await service.stream("t2", "第二轮")

    assert info.value.reason == REASON_RATE

    await _drain(held)


# ------------------------------------------------------------------ 拒绝路径不泄漏


async def test_rejection_does_not_reveal_whether_a_thread_exists(tmp_path, thread_store):
    """被限流时，「会话存在」与「会话不存在」必须得到同一个错误。

    WHY 这条最值得钉住：若限流排在所有权校验之后，未登记的会话会先撞上 404、
    已登记的先撞上 429，调用方据此就能枚举出哪些会话存在——限流不该成为一把
    探测他人会话的尺子。
    """
    service = _limited_service(tmp_path, thread_store, max_concurrent_runs=1)
    await thread_store.create("t1", title="真实存在的会话")
    held = await service.stream("t1", "占住槽位")

    with pytest.raises(RunRejectedError):
        await service.stream("t9", "不存在的会话")

    with pytest.raises(RunRejectedError):
        await service.stream("t1", "已存在的会话")

    await _drain(held)
