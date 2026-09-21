"""用量统计服务的单元测试。

覆盖点：时间窗与聚合维度的参数校验、汇总口径与按会话查询的存在性校验、
以及两类下游故障的降级语义。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Awaitable

import pytest

from application.errors import NotFoundError
from application.usage_service import UsageService
from runtime.usage_store import UsageStore
from tests.conftest import make_config


class StubThreadStore:
    """会话元数据存储替身：只实现用量服务用到的 ``get``。"""

    def __init__(self, records: dict[str, dict[str, Any]] | None = None) -> None:
        self._records = records or {}
        self.fail = False

    async def get(self, thread_id: str) -> dict[str, Any] | None:
        if self.fail:
            raise RuntimeError("数据库不可用")
        return self._records.get(thread_id)


def _run(coro: Awaitable[Any]) -> Any:
    """同步驱动一个协程。

    WHY 单独封装：参数校验类用例断言的是「await 之前就抛错」，用事件循环
    夹具反而把失败包装成 fixture 错误；直接 ``asyncio.run`` 更直观。
    """
    return asyncio.run(coro)


def _service(
    tmp_path: Path,
    usage_store: UsageStore,
    *,
    records: dict[str, dict[str, Any]] | None = None,
    **config_overrides: Any,
) -> tuple[UsageService, StubThreadStore]:
    config = make_config(tmp_path, **config_overrides)
    thread_store = StubThreadStore(records)
    return UsageService(config, usage_store=usage_store, thread_store=thread_store), thread_store


async def _seed(usage_store: UsageStore) -> None:
    await usage_store.record(
        thread_id="t1", owner_id="alice", model="deepseek-flash", prompt_tokens=100, completion_tokens=10
    )
    await usage_store.record(
        thread_id="t2", owner_id="bob", model="openai", prompt_tokens=20, completion_tokens=5
    )


# ------------------------------------------------------------------ 构造校验


def test_rejects_missing_dependencies(tmp_path: Path, usage_store: UsageStore):
    config = make_config(tmp_path)

    with pytest.raises(ValueError, match="usage_store"):
        UsageService(config, usage_store=None, thread_store=StubThreadStore())
    with pytest.raises(ValueError, match="thread_store"):
        UsageService(config, usage_store=usage_store, thread_store=None)
    with pytest.raises(ValueError, match="config"):
        UsageService(None, usage_store=usage_store, thread_store=StubThreadStore())


# ------------------------------------------------------------------ 窗口与维度


async def test_default_window_comes_from_config(tmp_path: Path, usage_store: UsageStore):
    service, _ = _service(tmp_path, usage_store, usage_default_window_days=3)
    await _seed(usage_store)

    summary = await service.summarize()

    assert summary.window_days == 3
    assert summary.group_by == "model"
    assert summary.thread_id is None
    assert summary.total_tokens == 135
    assert summary.run_count == 2
    assert summary.since  # 窗口起点必须回给调用方，否则前端无法解释数字口径


async def test_explicit_days_and_group_by(tmp_path: Path, usage_store: UsageStore):
    service, _ = _service(tmp_path, usage_store)
    await _seed(usage_store)

    summary = await service.summarize(days=1, group_by="thread")

    assert summary.window_days == 1
    assert {group.key for group in summary.groups} == {"t1", "t2"}
    # 按总量降序：t1 110 > t2 25
    assert summary.groups[0].key == "t1"
    assert summary.groups[0].total_tokens == 110


def test_rejects_bad_days(tmp_path: Path, usage_store: UsageStore):
    service, _ = _service(tmp_path, usage_store, usage_max_window_days=90)

    with pytest.raises(ValueError, match="days"):
        _run(service.summarize(days=0))
    with pytest.raises(ValueError, match="days"):
        _run(service.summarize(days=91))
    with pytest.raises(ValueError, match="days"):
        _run(service.summarize(days="7"))


def test_rejects_unknown_group_by(tmp_path: Path, usage_store: UsageStore):
    service, _ = _service(tmp_path, usage_store)

    with pytest.raises(ValueError, match="group_by"):
        _run(service.summarize(group_by="user"))
    with pytest.raises(ValueError, match="group_by"):
        _run(service.summarize(group_by="model; DROP TABLE usage_log;"))


def test_rejects_bad_thread_id(tmp_path: Path, usage_store: UsageStore):
    service, _ = _service(tmp_path, usage_store)

    with pytest.raises(ValueError, match="thread_id"):
        _run(service.summarize(thread_id="   "))
    with pytest.raises(ValueError, match="thread_id"):
        _run(service.summarize(thread_id=123))


# ------------------------------------------------------------------ 归属口径


async def test_summary_covers_every_run(tmp_path: Path, usage_store: UsageStore):
    """不区分主体：本机全部运行都计入同一份汇总。"""
    service, _ = _service(tmp_path, usage_store)
    await _seed(usage_store)

    summary = await service.summarize()

    assert summary.total_tokens == 135
    assert summary.run_count == 2


async def test_thread_scope_requires_known_thread(tmp_path: Path, usage_store: UsageStore):
    """按会话查询必须回读会话元数据：不存在的 ID 不能退化成「空汇总」。

    WHY：只按 ``thread_id`` 过滤会接受一个写错的 ID 并返回 0 条，而那与「这条会话确实
    没有用量」在响应上完全一样——调用方无从区分「没用量」与「ID 写错了」。
    """
    service, _ = _service(
        tmp_path,
        usage_store,
        records={"t1": {"thread_id": "t1"}},
    )
    await _seed(usage_store)

    with pytest.raises(NotFoundError):
        await service.summarize(thread_id="missing")

    scoped = await service.summarize(thread_id="t1")
    assert scoped.thread_id == "t1"
    assert scoped.total_tokens == 110


# ------------------------------------------------------------------ 故障降级


async def test_thread_store_failure_becomes_runtime_error(tmp_path: Path, usage_store: UsageStore):
    service, thread_store = _service(
        tmp_path,
        usage_store,
        records={"t1": {"thread_id": "t1"}},
    )
    thread_store.fail = True

    with pytest.raises(RuntimeError, match="读取会话元数据失败"):
        await service.summarize(thread_id="t1")


async def test_usage_store_failure_becomes_runtime_error(tmp_path: Path):
    """WHY 覆盖底层故障：SQLite 报错（锁、磁盘满）必须转成带上下文的
    RuntimeError 让路由回 500，而不是把原始 ``OperationalError`` 泄漏到
    调用栈之外。

    WHY 用替身而不是关掉真连接：aiosqlite 在连接已关闭时抛的是
    ValueError，会被「参数错误」分支误吞成 400。
    """
    class FailingUsageStore:
        async def summarize(self, **_: Any) -> dict[str, Any]:
            raise sqlite3.OperationalError("database is locked")

    config = make_config(tmp_path)
    service = UsageService(
        config, usage_store=FailingUsageStore(), thread_store=StubThreadStore()
    )

    with pytest.raises(RuntimeError, match="用量聚合失败"):
        await service.summarize()
