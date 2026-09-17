"""后台周期任务骨架的回归测试。

覆盖面：启动即执行、重复启动幂等、回调失败不退出循环、停止后不再执行、
停止幂等、无事件循环时的明确报错、构造校验。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from runtime.interval_worker import IntervalWorker


class RecordingCallback:
    """记录调用次数与参数的回调替身；可配置为前若干次抛错。"""

    def __init__(self, *, fail_times: int = 0, result: Any = "ok") -> None:
        self.calls = 0
        self.results: list[Any] = []
        self.fail_times = fail_times
        self.result = result

    async def __call__(self) -> Any:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"第 {self.calls} 次执行失败")
        self.results.append(self.result)
        return self.result


def _worker(callback: Any, **overrides: Any) -> IntervalWorker:
    params: dict[str, Any] = {"interval_seconds": 1}
    params.update(overrides)
    return IntervalWorker(callback, **params)


# ------------------------------------------------------------------ 构造校验


def test_constructor_rejects_invalid_args():
    callback = RecordingCallback()

    with pytest.raises(ValueError):
        IntervalWorker(None, interval_seconds=1)
    for bad in (0, -1, 1.5, True, "60"):
        with pytest.raises(ValueError):
            IntervalWorker(callback, interval_seconds=bad)
    for blank in ("", "   "):
        with pytest.raises(ValueError):
            IntervalWorker(callback, interval_seconds=1, name=blank)


def test_constructor_defaults():
    worker = _worker(RecordingCallback())

    assert worker.interval_seconds == 1
    assert worker.is_running is False
    assert worker.iterations == 0
    assert worker.failures == 0


# ------------------------------------------------------------------ 启停语义


async def test_start_runs_first_pass_immediately():
    """WHY 断言「启动即执行」：服务重启往往跟在长时停机之后，堆着的状态
    若等到下一个间隔才处理，首个间隔内系统仍处于陈旧状态。"""
    callback = RecordingCallback()
    worker = _worker(callback)

    worker.start()
    try:
        await asyncio.sleep(0.05)
        assert callback.calls == 1
        assert worker.is_running is True
        assert worker.iterations == 1
    finally:
        await worker.stop()


async def test_start_is_idempotent():
    callback = RecordingCallback()
    worker = _worker(callback)

    worker.start()
    worker.start()
    try:
        await asyncio.sleep(0.05)
        assert callback.calls == 1
    finally:
        await worker.stop()


async def test_stop_prevents_further_runs():
    callback = RecordingCallback()
    worker = _worker(callback, interval_seconds=60)

    worker.start()
    await asyncio.sleep(0.05)
    await worker.stop()
    await asyncio.sleep(0.05)

    assert callback.calls == 1
    assert worker.is_running is False


async def test_stop_is_noop_when_not_started():
    worker = _worker(RecordingCallback())

    await worker.stop()
    await worker.stop()

    assert worker.is_running is False


async def test_callback_failure_does_not_stop_loop():
    """WHY 断言循环存活：一次数据库抖动就永久停掉旁路运维任务，
    会让「以为在自动治理」变成静默的状态堆积。"""
    callback = RecordingCallback(fail_times=3)
    worker = _worker(callback, interval_seconds=60)

    worker.start()
    try:
        await asyncio.sleep(0.05)
        assert worker.is_running is True
        assert worker.failures == 1
        assert worker.iterations == 1
    finally:
        await worker.stop()


async def test_run_once_returns_callback_result():
    callback = RecordingCallback(result={"purged": 7})
    worker = _worker(callback)

    assert await worker.run_once() == {"purged": 7}
    assert callback.calls == 1
    assert worker.is_running is False


async def test_stop_releases_task_reference():
    """WHY 覆盖二次停止：lifespan 的 finally 可能与显式关闭路径重复执行，
    第二次 stop 必须安全返回而不是抛「任务已结束」。"""
    worker = _worker(RecordingCallback(), interval_seconds=60)

    worker.start()
    await worker.stop()
    await worker.stop()

    assert worker.is_running is False


def test_start_requires_running_loop():
    worker = _worker(RecordingCallback())

    with pytest.raises(RuntimeError, match="事件循环"):
        worker.start()


# ------------------------------------------------------------------ 周期推进


async def test_runs_again_after_interval():
    """WHY 需要一条真实等待间隔的用例：其余用例都在验证「启动/停止」，
    只有它会证明「sleep 之后确实还会再来一轮」——间隔参数一旦接错
    （例如忘了 sleep 或 sleep 了常数），前面的用例全都发现不了。"""
    callback = RecordingCallback()
    worker = _worker(callback, interval_seconds=1)

    worker.start()
    try:
        await asyncio.sleep(1.15)
        assert callback.calls == 2
    finally:
        await worker.stop()
