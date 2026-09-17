"""用量统计服务的单元测试。

覆盖点：时间窗与聚合维度的参数校验、三种认证模式下的 owner 收敛、
按会话查询时的归属校验（不存在 / 越权 / 正常）、以及两类下游故障的降级语义。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Awaitable

import pytest

from application.errors import NotFoundError, OwnershipError
from application.principal import Principal
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


_TEST_SESSION_SECRET = "测试用会话密钥" * 8


def _service(
    tmp_path: Path,
    usage_store: UsageStore,
    *,
    records: dict[str, dict[str, Any]] | None = None,
    **config_overrides: Any,
) -> tuple[UsageService, StubThreadStore]:
    overrides = dict(config_overrides)
    # WHY 只在开启认证时补密钥：配置校验要求非 disabled 模式必须有会话密钥，
    # 不补会让「切换 auth_mode」的用例集体构造失败，掩盖真正要测的归属逻辑。
    if overrides.get("auth_mode") not in (None, "disabled"):
        overrides.setdefault("auth_session_secret", _TEST_SESSION_SECRET)
    config = make_config(tmp_path, **overrides)
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
    # 认证关闭：不过滤 owner
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


# ------------------------------------------------------------------ 归属收敛


async def test_member_only_sees_own_usage(tmp_path: Path, usage_store: UsageStore):
    service, _ = _service(tmp_path, usage_store, auth_mode="apikey")
    await _seed(usage_store)

    summary = await service.summarize(Principal(user_id="alice", role="member"))

    assert summary.total_tokens == 110
    assert summary.run_count == 1


async def test_admin_sees_everyone(tmp_path: Path, usage_store: UsageStore):
    service, _ = _service(tmp_path, usage_store, auth_mode="apikey")
    await _seed(usage_store)

    summary = await service.summarize(Principal(user_id="root", role="admin"))

    assert summary.total_tokens == 135


async def test_thread_scope_requires_ownership(tmp_path: Path, usage_store: UsageStore):
    """WHY 这条是安全底线：只按 owner 过滤挡不住「猜别人的 thread_id
    直接查」，必须回读会话归属。"""
    service, _ = _service(
        tmp_path,
        usage_store,
        records={"t1": {"thread_id": "t1", "owner_id": "alice"}},
        auth_mode="apikey",
    )
    await _seed(usage_store)

    with pytest.raises(OwnershipError):
        await service.summarize(Principal(user_id="bob", role="member"), thread_id="t1")

    with pytest.raises(NotFoundError):
        await service.summarize(Principal(user_id="alice", role="member"), thread_id="missing")

    own = await service.summarize(Principal(user_id="alice", role="member"), thread_id="t1")
    assert own.thread_id == "t1"
    assert own.total_tokens == 110


async def test_admin_can_read_any_thread(tmp_path: Path, usage_store: UsageStore):
    service, _ = _service(
        tmp_path,
        usage_store,
        records={"t2": {"thread_id": "t2", "owner_id": "bob"}},
        auth_mode="apikey",
    )
    await _seed(usage_store)

    summary = await service.summarize(Principal(user_id="root", role="admin"), thread_id="t2")

    assert summary.total_tokens == 25


async def test_unauthenticated_principal_sees_nothing(tmp_path: Path, usage_store: UsageStore):
    """WHY 覆盖未认证：``owner_id`` 会取一个不可能匹配的常量，
    结果必须是 0 条，而不是退化成「不过滤」把所有人的用量都吐出去。"""
    service, _ = _service(tmp_path, usage_store, auth_mode="apikey")
    await _seed(usage_store)

    summary = await service.summarize(None)

    assert summary.run_count == 0


# ------------------------------------------------------------------ 故障降级


async def test_thread_store_failure_becomes_runtime_error(tmp_path: Path, usage_store: UsageStore):
    service, thread_store = _service(
        tmp_path,
        usage_store,
        records={"t1": {"thread_id": "t1", "owner_id": "alice"}},
        auth_mode="apikey",
    )
    thread_store.fail = True

    with pytest.raises(RuntimeError, match="读取会话元数据失败"):
        await service.summarize(Principal(user_id="alice", role="member"), thread_id="t1")


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
        await service.summarize(Principal(user_id="root", role="admin"))
