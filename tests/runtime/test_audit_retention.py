"""审计保留清理任务的回归测试。

覆盖面：单轮清理、启动后周期性执行、清理失败后的重试、停止时的资源回收、
构造参数校验与重复启动的幂等。
"""

from __future__ import annotations

import asyncio

import pytest

from runtime.audit_retention import AuditRetentionWorker


class FakeAuditStore:
    """可控的审计存储替身：记录调用次数，可注入失败。"""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[int] = []
        self._fail_times = fail_times

    async def purge_expired(self, *, retention_days: int) -> int:
        self.calls.append(retention_days)
        if len(self.calls) <= self._fail_times:
            raise RuntimeError("审计库暂时不可用")
        return len(self.calls)


def _worker(store: FakeAuditStore, *, interval: int = 60) -> AuditRetentionWorker:
    return AuditRetentionWorker(store, retention_days=30, interval_seconds=interval)


# ------------------------------------------------------------------ 构造校验


def test_constructor_rejects_none_store():
    with pytest.raises(ValueError):
        AuditRetentionWorker(None, retention_days=30, interval_seconds=60)


@pytest.mark.parametrize("days", [0, -1])
def test_constructor_rejects_bad_retention_days(days):
    with pytest.raises(ValueError):
        AuditRetentionWorker(FakeAuditStore(), retention_days=days, interval_seconds=60)


@pytest.mark.parametrize("interval", [0, -5])
def test_constructor_rejects_bad_interval(interval):
    with pytest.raises(ValueError):
        AuditRetentionWorker(FakeAuditStore(), retention_days=30, interval_seconds=interval)


@pytest.mark.parametrize("days", ["30", 1.5, None])
def test_constructor_rejects_non_integer_retention_days(days):
    with pytest.raises(ValueError):
        AuditRetentionWorker(FakeAuditStore(), retention_days=days, interval_seconds=60)


# ------------------------------------------------------------------ 生命周期


async def test_prune_once_delegates_retention_days():
    store = FakeAuditStore()
    worker = _worker(store)

    assert await worker.prune_once() == 1
    assert store.calls == [30]


async def test_start_runs_immediately_and_then_periodically():
    store = FakeAuditStore()
    worker = _worker(store, interval=1)

    worker.start()
    try:
        # 启动即执行一次，不必等第一个间隔
        await asyncio.sleep(0.05)
        assert len(store.calls) == 1

        await asyncio.sleep(1.1)
        assert len(store.calls) == 2
    finally:
        await worker.stop()


async def test_start_is_idempotent():
    store = FakeAuditStore()
    worker = _worker(store, interval=60)

    worker.start()
    worker.start()
    try:
        assert worker.is_running is True
        # 让协程真正跑起来，否则 stop 会把它取消在第一次清理之前
        await asyncio.sleep(0.05)
    finally:
        await worker.stop()

    # 重复 start 不得起第二个协程：否则同一张表会被并发 DELETE
    assert worker.is_running is False
    assert len(store.calls) == 1


async def test_prune_failure_does_not_stop_the_loop():
    """WHY 覆盖失败路径：一次数据库抖动就永久停掉清理任务，
    会让「以为在自动清理」变成静默的磁盘增长。"""
    store = FakeAuditStore(fail_times=1)
    worker = _worker(store, interval=1)

    worker.start()
    try:
        await asyncio.sleep(1.2)
        assert len(store.calls) >= 2
        assert store.calls[0] == 30
    finally:
        await worker.stop()


async def test_stop_is_safe_without_start():
    worker = _worker(FakeAuditStore())

    await worker.stop()
    await worker.stop()

    assert worker.is_running is False


async def test_stop_cancels_pending_sleep():
    store = FakeAuditStore()
    worker = _worker(store, interval=3600)

    worker.start()
    await asyncio.sleep(0.05)
    await worker.stop()

    assert worker.is_running is False
    # 停止不得触发额外的清理：只在启动与每个间隔结束时执行
    assert len(store.calls) == 1
