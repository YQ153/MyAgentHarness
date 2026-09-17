"""会话元数据存储的回归测试。

覆盖面：建表幂等、轮次登记（UPSERT 语义：标题/所有者只写一次）、
活动时间刷新、删除、按所有者过滤的列表与计数、分页参数校验，
以及重命名、归档（软删除）、标题搜索与老库升级路径。
"""

from __future__ import annotations

import aiosqlite
import pytest

import runtime.thread_store as thread_store_module
from runtime.thread_store import (
    ThreadMetaStore,
    open_thread_store,
)


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


# ------------------------------------------------------------------ 重命名


async def test_rename_changes_title_only(thread_store):
    """改名不是对话活动：``updated_at`` 不能被刷新，否则整理标题会打乱列表顺序。"""
    created = await thread_store.create("t1", title="旧标题")
    await thread_store.record_turn("t1", turn_delta=1)
    before = await thread_store.get("t1")

    renamed = await thread_store.rename("t1", "新标题")

    assert renamed["title"] == "新标题"
    assert renamed["updated_at"] == before["updated_at"]
    assert renamed["created_at"] == created["created_at"]
    assert renamed["turn_count"] == before["turn_count"]


async def test_rename_collapses_whitespace(thread_store):
    await thread_store.create("t1", title="旧")

    renamed = await thread_store.rename("t1", "  多  行\n标题  ")

    assert renamed["title"] == "多 行 标题"


async def test_rename_rejects_blank_and_overlong(thread_store):
    await thread_store.create("t1", title="旧")

    with pytest.raises(ValueError, match="标题不能为空"):
        await thread_store.rename("t1", "   ")
    with pytest.raises(ValueError, match="标题过长"):
        await thread_store.rename("t1", "x" * 201)


async def test_rename_missing_thread_returns_none(thread_store):
    assert await thread_store.rename("ghost", "新标题") is None


# ------------------------------------------------------------------ 归档


async def test_archive_sets_flag_and_timestamp(thread_store):
    await thread_store.create("t1", title="会话")

    archived = await thread_store.set_archived("t1", True)

    assert archived["archived"] is True
    assert archived["archived_at"]


async def test_restore_clears_archived_timestamp(thread_store):
    """恢复后必须清掉 ``archived_at``，否则会留下「已恢复但仍有归档时间」的矛盾记录。"""
    await thread_store.create("t1")
    await thread_store.set_archived("t1", True)

    restored = await thread_store.set_archived("t1", False)

    assert restored["archived"] is False
    assert restored["archived_at"] == ""


async def test_archive_does_not_touch_activity_time(thread_store):
    await thread_store.create("t1")
    before = await thread_store.get("t1")

    archived = await thread_store.set_archived("t1", True)

    assert archived["updated_at"] == before["updated_at"]


async def test_set_archived_validates_arguments(thread_store):
    await thread_store.create("t1")

    with pytest.raises(ValueError, match="archived"):
        await thread_store.set_archived("t1", "yes")
    with pytest.raises(ValueError):
        await thread_store.set_archived("", True)


async def test_set_archived_missing_thread_returns_none(thread_store):
    assert await thread_store.set_archived("ghost", True) is None


# ------------------------------------------------------------------ 搜索与过滤


async def test_list_hides_archived_by_default(thread_store):
    """归档的语义就是「从清单里收起来」；默认仍返回等于这个功能没做。"""
    await thread_store.create("keep", title="保留")
    await thread_store.create("hidden", title="收起")
    await thread_store.set_archived("hidden", True)

    default_ids = [row["thread_id"] for row in await thread_store.list_threads()]
    all_ids = [row["thread_id"] for row in await thread_store.list_threads(include_archived=True)]

    assert default_ids == ["keep"]
    assert sorted(all_ids) == ["hidden", "keep"]


async def test_count_matches_list_filters(thread_store):
    """总数与条数必须同口径，否则前端显示「还有下一页」却翻不到东西。"""
    await thread_store.create("keep", title="保留")
    await thread_store.create("hidden", title="收起")
    await thread_store.set_archived("hidden", True)

    assert await thread_store.count() == 1
    assert await thread_store.count(include_archived=True) == 2


async def test_search_matches_title_substring(thread_store):
    await thread_store.create("t1", title="排查登录超时")
    await thread_store.create("t2", title="写周报")

    rows = await thread_store.list_threads(query="登录")

    assert [row["thread_id"] for row in rows] == ["t1"]
    assert await thread_store.count(query="登录") == 1


async def test_search_escapes_like_wildcards(thread_store):
    """``%`` 与 ``_`` 是 LIKE 元字符：不转义会「搜什么都能搜到」。"""
    await thread_store.create("t1", title="命中 50% 的任务")
    await thread_store.create("t2", title="完全无关")

    assert [row["thread_id"] for row in await thread_store.list_threads(query="50%")] == ["t1"]

    # 下划线同理：a_b 不应匹配 axb
    await thread_store.create("t3", title="axb")
    assert await thread_store.list_threads(query="a_b") == []


async def test_search_blank_query_means_no_filter(thread_store):
    await thread_store.create("t1", title="任意标题")

    assert len(await thread_store.list_threads(query="   ")) == 1
    assert len(await thread_store.list_threads(query=None)) == 1


async def test_search_validates_query(thread_store):
    with pytest.raises(ValueError, match="query 过长"):
        await thread_store.list_threads(query="x" * 201)
    with pytest.raises(ValueError, match="query 必须是字符串"):
        await thread_store.list_threads(query=123)
    with pytest.raises(ValueError, match="query 过长"):
        await thread_store.count(query="x" * 201)


async def test_search_combines_with_owner_filter(thread_store):
    await thread_store.create("a1", title="登录问题", owner_id="alice")
    await thread_store.create("b1", title="登录问题", owner_id="bob")

    rows = await thread_store.list_threads(owner_id="alice", query="登录")

    assert [row["thread_id"] for row in rows] == ["a1"]


# ------------------------------------------------------------------ 老库升级


async def test_open_migrates_legacy_table(tmp_path):
    """升级路径：老库没有归档列，打开时必须自动补列，且历史会话默认可见。"""
    db_path = tmp_path / "legacy.db"
    conn = await aiosqlite.connect(str(db_path))
    try:
        await conn.executescript(
            """
            CREATE TABLE thread_meta (
                thread_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                turn_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await conn.execute(
            """
            INSERT INTO thread_meta
                (thread_id, owner_id, title, created_at, updated_at, turn_count)
            VALUES ('legacy', 'alice', '历史会话',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 3)
            """
        )
        await conn.commit()
    finally:
        await conn.close()

    async with open_thread_store(db_path) as store:
        record = await store.get("legacy")
        rows = await store.list_threads()

    assert record is not None
    assert record["archived"] is False
    assert record["archived_at"] == ""
    assert [row["thread_id"] for row in rows] == ["legacy"]
