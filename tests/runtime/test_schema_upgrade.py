"""老库升级路径的回归测试：升级上来的库必须能直接启动。

WHY 需要这组用例：其余所有测试都用**全新**库，建表时就带着最新列，于是「补列」这条
路径从未被走到。而真实的升级场景恰恰相反——库里已有数据，`CREATE TABLE IF NOT EXISTS`
不做任何事，任何依赖新列的语句（尤其是建在新列上的索引）若排在补列之前，就会以
``no such column`` 失败，而那是**启动即失败**，不是某个功能不可用。

这条路径已经真实翻过一次车：trace_id 的索引曾写在建表脚本里、补列的 ALTER 在其后，
导致升级库启动崩在 ``executescript`` 上，而当时全量测试是绿的。
"""

from __future__ import annotations

import sqlite3

import pytest

from runtime.audit_store import open_audit_store
from runtime.usage_store import open_usage_store

_OLD_AUDIT_SCHEMA = """
CREATE TABLE audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT NOT NULL,
    actor_id      TEXT NOT NULL,
    target_id     TEXT,
    action        TEXT,
    outcome       TEXT NOT NULL,
    ip            TEXT,
    user_agent    TEXT,
    details       TEXT,
    created_at    TEXT NOT NULL
);
"""

_OLD_USAGE_SCHEMA = """
CREATE TABLE usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id         TEXT NOT NULL,
    owner_id          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);
"""


def _make_legacy_db(path, schema: str) -> None:
    """造一个升级前的库：只有旧列、且已有一条数据。"""
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    connection.commit()
    connection.close()


def _columns(path, table: str) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        cursor = connection.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}
    finally:
        connection.close()


async def test_audit_store_upgrades_a_legacy_database(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path, _OLD_AUDIT_SCHEMA)

    async with open_audit_store(db_path) as store:
        await store.log(event_type="t", actor_id="a", outcome="success", trace_id="trace-1")
        assert "trace_id" in _columns(db_path, "audit_log")


async def test_usage_store_upgrades_a_legacy_database(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path, _OLD_USAGE_SCHEMA)

    async with open_usage_store(db_path) as store:
        await store.record(
            thread_id="t1",
            model="deepseek-flash",
            prompt_tokens=1,
            completion_tokens=2,
            trace_id="trace-1",
        )
        assert "trace_id" in _columns(db_path, "usage_log")


async def test_opening_twice_is_idempotent(tmp_path):
    """重复打开（每次都会跑一遍迁移）不得出错。"""
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path, _OLD_AUDIT_SCHEMA)

    for _ in range(2):
        async with open_audit_store(db_path):
            pass

    assert "trace_id" in _columns(db_path, "audit_log")


@pytest.mark.parametrize("store_factory", [open_audit_store, open_usage_store])
async def test_fresh_database_also_has_the_column(tmp_path, store_factory):
    """全新库同样不能漏掉新列——两条路径都要覆盖。"""
    db_path = tmp_path / "fresh.db"

    async with store_factory(db_path):
        pass

    table = "audit_log" if store_factory is open_audit_store else "usage_log"
    assert "trace_id" in _columns(db_path, table)
