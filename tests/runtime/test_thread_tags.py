"""会话标签的存储与过滤回归测试。

覆盖面：标签往返、规范化（去空白 / 去重 / 拒绝逗号与超长与超量）、清空语义、
**按标签过滤时总数与条数口径一致**。

WHY 用真实存储而不是替身：本文件要验的正是「存入的规范形式」与「过滤用的模式」
是否同源——替身会把这两件事都跳过，于是最该被验的那一处恰好没被验到。
"""

from __future__ import annotations

import pytest

from runtime.thread_store import (
    normalize_tags,
    open_thread_store,
    tags_from_storage,
    tags_to_storage,
)


# ------------------------------------------------------------------ 编解码


def test_tags_round_trip_through_storage():
    assert tags_to_storage(["a", "b"]) == ",a,b,"
    assert tags_from_storage(",a,b,") == ["a", "b"]
    assert tags_to_storage([]) == ""
    assert tags_from_storage("") == []


def test_normalize_trims_dedups_and_keeps_order():
    assert normalize_tags(["  a  ", "b", "a", "", "  "]) == ["a", "b"]


def test_normalize_none_is_empty():
    assert normalize_tags(None) == []


@pytest.mark.parametrize(
    "bad",
    [
        ["a,b"],  # 逗号会把一个标签拆成两个
        ["x" * 33],  # 超长
        [str(index) for index in range(11)],  # 超量
        [123],  # 非字符串
        "not-a-list",
    ],
)
def test_normalize_rejects_invalid(bad):
    with pytest.raises(ValueError):
        normalize_tags(bad)


# ------------------------------------------------------------------ 读写


async def test_set_and_read_tags(tmp_path):
    async with open_thread_store(tmp_path / "a.db") as store:
        await store.create("t1", title="会话")

        updated = await store.set_tags("t1", ["重要", " 待办 "])

        assert updated is not None
        assert updated["tags"] == ["重要", "待办"]
        assert (await store.get("t1"))["tags"] == ["重要", "待办"]


async def test_empty_list_clears_tags(tmp_path):
    async with open_thread_store(tmp_path / "a.db") as store:
        await store.create("t1", title="会话")
        await store.set_tags("t1", ["a"])

        await store.set_tags("t1", [])

        assert (await store.get("t1"))["tags"] == []


async def test_set_tags_on_missing_thread_returns_none(tmp_path):
    async with open_thread_store(tmp_path / "a.db") as store:
        assert await store.set_tags("never-existed", ["a"]) is None


# ------------------------------------------------------------------ 过滤


async def test_tag_filter_keeps_total_and_items_consistent(tmp_path):
    """按标签过滤时，总数与实际返回条数必须一致。

    WHY 单列一条：这两者在 T10 就吃过亏——过滤条件各写一份时，分页元信息会与实际
    条数分叉，表现成「显示还有下一页，翻过去却是空的」。标签是新增的第三个条件，
    最容易在「只改了 list 忘了 count」时重演同一个问题。
    """
    async with open_thread_store(tmp_path / "a.db") as store:
        await store.create("t1", title="一")
        await store.create("t2", title="二")
        await store.create("t3", title="三")
        await store.set_tags("t1", ["工作"])
        await store.set_tags("t2", ["工作", "紧急"])
        await store.set_tags("t3", ["生活"])

        items = await store.list_threads(tag="工作", limit=50)
        total = await store.count(tag="工作")

        assert {item["thread_id"] for item in items} == {"t1", "t2"}
        assert total == len(items) == 2


async def test_tag_filter_matches_whole_tag_only(tmp_path):
    """标签必须整体匹配：``tag`` 不该命中 ``mytag``。"""
    async with open_thread_store(tmp_path / "a.db") as store:
        await store.create("t1", title="一")
        await store.set_tags("t1", ["mytag"])

        assert await store.count(tag="tag") == 0
        assert await store.count(tag="mytag") == 1


async def test_tag_filter_escapes_wildcards(tmp_path):
    """含 ``%`` 的标签不得把整张表都匹配上。"""
    async with open_thread_store(tmp_path / "a.db") as store:
        await store.create("t1", title="一")
        await store.create("t2", title="二")
        await store.set_tags("t1", ["50%"])

        assert await store.count(tag="50%") == 1
        assert await store.count(tag="%") == 0
