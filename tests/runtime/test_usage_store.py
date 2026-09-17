"""用量存储与聚合的集成测试（真实 SQLite 文件）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from runtime.usage_store import UsageStore, open_usage_store, utc_now, window_start


@pytest.fixture
async def store(tmp_path: Path):
    async with open_usage_store(tmp_path / "usage.db") as opened:
        yield opened


# ------------------------------------------------------------------ 写入


async def test_record_returns_id_and_counts(store: UsageStore):
    row_id = await store.record(thread_id="t1", model="deepseek-flash", prompt_tokens=10, completion_tokens=2)

    assert row_id > 0
    assert await store.count_all() == 1


async def test_record_defaults_owner_and_timestamp(store: UsageStore):
    """WHY 覆盖缺省：认证关闭时没有 owner_id，落库必须是空串而不是 NULL，
    否则按 owner 聚合的行会被 SQL 排除在外。"""
    await store.record(thread_id="t1", model="m", prompt_tokens=1, completion_tokens=1)

    summary = await store.summarize(group_by="thread")
    assert summary["groups"][0]["key"] == "t1"
    assert summary["run_count"] == 1


async def test_record_rejects_invalid_arguments(store: UsageStore):
    with pytest.raises(ValueError, match="thread_id"):
        await store.record(thread_id="  ", model="m", prompt_tokens=0, completion_tokens=0)
    with pytest.raises(ValueError, match="负"):
        await store.record(thread_id="t", model="m", prompt_tokens=-1, completion_tokens=0)
    with pytest.raises(ValueError, match="上限"):
        await store.record(thread_id="t", model="m", prompt_tokens=0, completion_tokens=10**9)
    with pytest.raises(ValueError, match="model 必须是"):
        await store.record(thread_id="t", model=None, prompt_tokens=0, completion_tokens=0)


async def test_record_propagates_db_failure(store: UsageStore):
    """WHY 覆盖异常路径：用量写库失败必须上冒，由调用方决定降级策略，
    存储层不能把「写失败」伪装成「写成功」。"""
    await store._conn.close()

    with pytest.raises(Exception):
        await store.record(thread_id="t", model="m", prompt_tokens=1, completion_tokens=1)


# ------------------------------------------------------------------ 聚合


async def _seed(store: UsageStore) -> None:
    await store.record(thread_id="t1", owner_id="alice", model="deepseek-flash", prompt_tokens=100, completion_tokens=10)
    await store.record(thread_id="t1", owner_id="alice", model="openai", prompt_tokens=50, completion_tokens=5)
    await store.record(thread_id="t2", owner_id="bob", model="deepseek-flash", prompt_tokens=7, completion_tokens=3)


async def test_summarize_totals_and_group_by_model(store: UsageStore):
    await _seed(store)

    summary = await store.summarize(group_by="model")

    assert summary["prompt_tokens"] == 157
    assert summary["completion_tokens"] == 18
    assert summary["total_tokens"] == 175
    assert summary["run_count"] == 3
    # 按总量降序：deepseek-flash 120 > openai 55
    assert [group["key"] for group in summary["groups"]] == ["deepseek-flash", "openai"]
    assert summary["groups"][0]["total_tokens"] == 120


async def test_summarize_group_by_thread_and_day(store: UsageStore):
    await _seed(store)

    by_thread = await store.summarize(group_by="thread")
    assert {group["key"] for group in by_thread["groups"]} == {"t1", "t2"}

    by_day = await store.summarize(group_by="day")
    assert by_day["groups"][0]["key"] == utc_now()[:10]


async def test_summarize_filters_owner_and_thread(store: UsageStore):
    await _seed(store)

    alice = await store.summarize(owner_id="alice")
    assert alice["run_count"] == 2
    assert alice["total_tokens"] == 165

    bob_thread = await store.summarize(owner_id="bob", thread_id="t2")
    assert bob_thread["run_count"] == 1
    assert bob_thread["total_tokens"] == 10

    # owner 与 thread 不匹配时结果必须为空，而不是退化成「只按 owner 过滤」
    assert (await store.summarize(owner_id="alice", thread_id="t2"))["run_count"] == 0


async def test_summarize_time_window(store: UsageStore):
    await store.record(
        thread_id="t1",
        model="m",
        prompt_tokens=1,
        completion_tokens=1,
        created_at="2000-01-01T00:00:00+00:00",
    )
    await _seed(store)

    summary = await store.summarize(since="2020-01-01T00:00:00+00:00")
    assert summary["run_count"] == 3

    all_rows = await store.summarize()
    assert all_rows["run_count"] == 4


async def test_summarize_empty_table(store: UsageStore):
    summary = await store.summarize()

    assert summary == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "run_count": 0,
        "groups": [],
    }


async def test_summarize_rejects_bad_arguments(store: UsageStore):
    with pytest.raises(ValueError, match="group_by"):
        await store.summarize(group_by="model; DROP TABLE usage_log;")
    with pytest.raises(ValueError, match="since"):
        await store.summarize(since="  ")


# ------------------------------------------------------------------ 工具函数


def test_window_start_moves_back_in_time():
    now = utc_now()
    assert window_start(1) < now
    assert window_start(7) < window_start(1)
    assert window_start(1)[-6:] == "+00:00"


@pytest.mark.parametrize("days", [0, -1, "7", 1.5, True])
def test_window_start_rejects_invalid_days(days):
    with pytest.raises(ValueError):
        window_start(days)


async def test_concurrent_writes_are_serialized(tmp_path: Path):
    """WHY 覆盖并发：所有写语句在同一把锁下执行，不测就没人保证
    「并发写不会撞上 SQLite 的写锁或让计数串行化失效」。"""
    async with open_usage_store(tmp_path / "concurrent.db") as store:
        await asyncio.gather(
            *[
                store.record(thread_id=f"t{index}", model="m", prompt_tokens=1, completion_tokens=1)
                for index in range(20)
            ]
        )

        assert await store.count_all() == 20


async def test_open_usage_store_closes_connection(tmp_path: Path):
    db_path = tmp_path / "nested" / "usage.db"
    async with open_usage_store(db_path) as store:
        await store.record(thread_id="t", model="m", prompt_tokens=1, completion_tokens=1)

    assert db_path.exists()
    with pytest.raises(Exception):
        await store.record(thread_id="t", model="m", prompt_tokens=1, completion_tokens=1)
