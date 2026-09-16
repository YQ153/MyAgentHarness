"""会话元数据存储的回归测试。

覆盖面：ID 规范化、建表幂等、轮次登记（UPSERT 语义：标题/所有者只写一次）、
活动时间刷新、删除、按所有者过滤的列表与计数、分页参数校验。
"""

from __future__ import annotations

import pytest

import runtime.thread_store as thread_store_module
from runtime.thread_store import (
    MAX_THREAD_ID_CHARS,
    ThreadMetaStore,
    normalize_thread_id,
    open_thread_store,
)


# ------------------------------------------------------------------ ID 规范化


def test_normalize_strips_whitespace():
    assert normalize_thread_id("  abc  ") == "abc"


def test_normalize_rejects_empty():
    with pytest.raises(ValueError):
        normalize_thread_id("   ")


def test_normalize_rejects_non_string():
    with pytest.raises(ValueError):
        normalize_thread_id(12345)


def test_normalize_rejects_overlong():
    with pytest.raises(ValueError):
        normalize_thread_id("x" * (MAX_THREAD_ID_CHARS + 1))


def test_normalize_accepts_max_length():
    assert normalize_thread_id("x" * MAX_THREAD_ID_CHARS) == "x" * MAX_THREAD_ID_CHARS


# ------------------------------------------------------------------ 写入


async def test_create_and_get_roundtrip(thread_store):
    record = await thread_store.create("t1", title="hello", owner_id="alice")

    assert record["thread_id"] == "t1"
    assert record["owner_id"] == "alice"
    assert record["title"] == "hello"
    assert record["turn_count"] == 0
    assert record["created_at"] == record["updated_at"]

    fetched = await thread_store.get("t1")
    assert fetched == record


async def test_create_is_idempotent(thread_store):
    await thread_store.create("t1", title="first", owner_id="alice")
    second = await thread_store.create("t1", title="second", owner_id="bob")

    # 已存在时保持原记录：标题与所有者都不能被第二次 create 覆盖
    assert second["title"] == "first"
    assert second["owner_id"] == "alice"


async def test_get_unknown_returns_none(thread_store):
    assert await thread_store.get("nope") is None


async def test_create_rejects_invalid_thread_id(thread_store):
    with pytest.raises(ValueError):
        await thread_store.create("")


async def test_record_turn_autocreates_row(thread_store):
    record = await thread_store.record_turn(
        "t1", title_hint="hello world", turn_delta=1, owner_id="alice"
    )

    assert record is not None
    assert record["turn_count"] == 1
    assert record["title"] == "hello world"
    assert record["owner_id"] == "alice"


async def test_record_turn_keeps_owner_once_claimed(thread_store):
    await thread_store.record_turn("t1", title_hint="hi", turn_delta=1, owner_id="alice")

    record = await thread_store.record_turn(
        "t1", title_hint="again", turn_delta=1, owner_id="bob"
    )

    # WHY 关键安全语义：所有者一经写入不可被后续登记覆盖，
    # 否则认证开启后任何用户都能「偷走」他人会话。
    assert record["owner_id"] == "alice"


async def test_record_turn_keeps_first_title(thread_store):
    await thread_store.record_turn("t1", title_hint="first title", turn_delta=1)

    record = await thread_store.record_turn(
        "t1", title_hint="second title", turn_delta=1
    )

    assert record["title"] == "first title"
    assert record["turn_count"] == 2


async def test_record_turn_validates_delta(thread_store):
    for bad in (-1, 101, "x", 1.5, True):
        with pytest.raises(ValueError):
            await thread_store.record_turn("t1", turn_delta=bad)


async def test_record_turn_unmatched_returns_none(thread_store):
    # 参数非法（turn_delta=-1）时 UPSERT 不会执行；这里用「合法但未命中」
    # 的路径不存在——record_turn 总会自动补行，因此用非法 ID 验证校验先行
    with pytest.raises(ValueError):
        await thread_store.record_turn("", turn_delta=1)


async def test_touch_updates_time_only(thread_store):
    await thread_store.create("t1", title="hello")

    assert await thread_store.touch("t1") is True
    record = await thread_store.get("t1")
    assert record["turn_count"] == 0
    assert record["title"] == "hello"


async def test_touch_unknown_returns_false(thread_store):
    assert await thread_store.touch("nope") is False


async def test_delete_thread(thread_store):
    await thread_store.create("t1")

    assert await thread_store.delete("t1") is True
    assert await thread_store.get("t1") is None
    assert await thread_store.delete("t1") is False


async def test_delete_rejects_invalid_thread_id(thread_store):
    with pytest.raises(ValueError):
        await thread_store.delete("")


async def test_title_truncated_to_storage_hard_limit(thread_store):
    record = await thread_store.create("t1", title="字" * 300)

    assert len(record["title"]) <= 200
    assert record["title"].endswith("…")


# ------------------------------------------------------------------ 读取


async def test_list_orders_by_recent_activity(thread_store, monkeypatch):
    # WHY 冻结时钟：updated_at 是秒级精度，同一秒内创建的会话只能靠
    # thread_id 决胜，注入递增时间才能让排序断言稳定。
    timestamps = iter(
        [
            "2026-01-01T00:00:01+00:00",
            "2026-01-01T00:00:02+00:00",
            "2026-01-01T00:00:03+00:00",
        ]
    )
    monkeypatch.setattr(
        thread_store_module, "_utc_now", lambda: next(timestamps, "2026-01-01T00:00:03+00:00")
    )

    await thread_store.create("older")
    await thread_store.create("newer")
    await thread_store.touch("older")  # older 的活动时间被刷新到最新

    listed = await thread_store.list_threads()
    assert [item["thread_id"] for item in listed] == ["older", "newer"]


async def test_list_filters_by_owner(thread_store):
    await thread_store.create("a", owner_id="alice")
    await thread_store.create("b", owner_id="bob")
    await thread_store.create("legacy")  # owner_id=''

    alice_only = await thread_store.list_threads(owner_id="alice")
    assert [item["thread_id"] for item in alice_only] == ["a"]

    with_legacy = await thread_store.list_threads(owner_id="alice", include_unowned=True)
    assert {item["thread_id"] for item in with_legacy} == {"a", "legacy"}

    everyone = await thread_store.list_threads()
    assert {item["thread_id"] for item in everyone} == {"a", "b", "legacy"}


async def test_list_paging(thread_store):
    for index in range(5):
        await thread_store.create(f"t{index}")

    page = await thread_store.list_threads(limit=2, offset=1)
    assert len(page) == 2


async def test_list_validates_paging(thread_store):
    for limit in (0, 201, "x", True):
        with pytest.raises(ValueError):
            await thread_store.list_threads(limit=limit)
    with pytest.raises(ValueError):
        await thread_store.list_threads(offset=-1)


async def test_count_filters_by_owner(thread_store):
    await thread_store.create("a", owner_id="alice")
    await thread_store.create("b", owner_id="bob")
    await thread_store.create("legacy")

    assert await thread_store.count() == 3
    assert await thread_store.count(owner_id="alice") == 1
    assert await thread_store.count(owner_id="alice", include_unowned=True) == 2


async def test_open_thread_store_rejects_none_path():
    with pytest.raises(ValueError):
        async with open_thread_store(None):
            pass


async def test_store_rejects_none_connection():
    with pytest.raises(ValueError):
        ThreadMetaStore(None)
