"""长期记忆存储的持久化测试（真实 SQLite 文件）。

WHY 必须有「关掉再打开」的用例：``InMemoryStore`` 时代「跨会话记忆」只在同一
进程内成立，而本项的验收标准是**重启不丢**——只有真的把连接关掉、重新建立
一次，才能证明数据落在了磁盘上，而不是仍躺在某个进程内的字典里。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from runtime.store import open_store

_ALICE = ("memories", "alice")


async def test_memory_survives_reopen(tmp_path: Path):
    db_path = tmp_path / "agent.db"

    async with open_store(db_path) as store:
        await store.aput(_ALICE, "/notes.md", {"content": "用户偏好中文", "encoding": "utf-8"})

    async with open_store(db_path) as store:
        items = await store.asearch(_ALICE)

    assert [item.key for item in items] == ["/notes.md"]
    assert items[0].value["content"] == "用户偏好中文"


async def test_delete_survives_reopen(tmp_path: Path):
    """删除也要落在磁盘上：否则重启后「已忘掉」的记忆会复活。"""
    db_path = tmp_path / "agent.db"
    async with open_store(db_path) as store:
        await store.aput(_ALICE, "/notes.md", {"content": "x", "encoding": "utf-8"})
        await store.adelete(_ALICE, "/notes.md")

    async with open_store(db_path) as store:
        assert await store.asearch(_ALICE) == []


async def test_namespaces_stay_isolated_on_disk(tmp_path: Path):
    db_path = tmp_path / "agent.db"
    async with open_store(db_path) as store:
        await store.aput(_ALICE, "/notes.md", {"content": "alice", "encoding": "utf-8"})
        await store.aput(("memories", "bob"), "/notes.md", {"content": "bob", "encoding": "utf-8"})

    async with open_store(db_path) as store:
        alice = await store.asearch(_ALICE)
        bob = await store.asearch(("memories", "bob"))

    assert [item.value["content"] for item in alice] == ["alice"]
    assert [item.value["content"] for item in bob] == ["bob"]


async def test_store_enables_wal(tmp_path: Path):
    """WAL 必须显式开启。

    WHY 覆盖它：Agent 写记忆发生在运行中途，而此时审计与用量表也在写同一个
    文件；WAL 是库级持久属性，因此可以从另一个连接读回来验证。默认的
    ``delete`` 日志模式会让并发读写频繁互锁，表现为「记忆偶发写不进去」。

    注意 ``busy_timeout`` 是**连接级**属性，无法从新连接读回，因此这里不
    断言它——用一条恒真断言充数只会让人误以为覆盖到了。
    """
    db_path = tmp_path / "agent.db"
    async with open_store(db_path):
        pass

    conn = await aiosqlite.connect(str(db_path))
    try:
        journal = await (await conn.execute("PRAGMA journal_mode;")).fetchone()
    finally:
        await conn.close()

    assert str(journal[0]).lower() == "wal"


async def test_store_creates_parent_directory(tmp_path: Path):
    db_path = tmp_path / "nested" / "deep" / "agent.db"

    async with open_store(db_path):
        pass

    assert db_path.exists()


async def test_store_rejects_none_path():
    with pytest.raises(ValueError, match="db_path"):
        async with open_store(None):  # type: ignore[arg-type]
            pass
