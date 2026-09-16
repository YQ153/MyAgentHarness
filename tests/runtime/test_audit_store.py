"""审计日志存储的回归测试。

覆盖面：写入与查询、IP/UA 落库、保留期清理（边界与参数校验）、
参数越界与非法输入。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from runtime.audit_store import AuditStore


async def _insert_dated(store: AuditStore, *, created_at: str, actor: str = "alice") -> None:
    """绕过 ``log`` 直接插入一条指定时间的记录，用于构造超期数据。"""
    async with store._lock:  # noqa: SLF001 测试需要复用存储层的锁与连接
        await store._conn.execute(  # noqa: SLF001
            """
            INSERT INTO audit_log (event_type, actor_id, target_id, action, outcome, ip, user_agent, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("thread_run", actor, "t1", "stream", "success", "10.0.0.1", "ua", None, created_at),
        )
        await store._conn.commit()


# ------------------------------------------------------------------ 写入与查询


async def test_log_and_list_roundtrip(audit_store):
    await audit_store.log(
        event_type="thread_run",
        actor_id="alice",
        target_id="t1",
        action="stream",
        outcome="success",
        ip="198.51.100.1",
        user_agent="pytest-agent",
        details={"model": "deepseek-flash"},
    )

    rows = await audit_store.list(actor_id="alice")

    assert len(rows) == 1
    assert rows[0]["event_type"] == "thread_run"
    assert rows[0]["ip"] == "198.51.100.1"
    assert rows[0]["user_agent"] == "pytest-agent"
    assert rows[0]["details"] == '{"model": "deepseek-flash"}'


async def test_log_without_client_info_writes_null(audit_store):
    await audit_store.log(event_type="thread_run", actor_id="bob", outcome="success")

    rows = await audit_store.list(actor_id="bob")

    assert rows[0]["ip"] is None
    assert rows[0]["user_agent"] is None


async def test_list_filters_by_event_type(audit_store):
    await audit_store.log(event_type="thread_run", actor_id="alice", outcome="success")
    await audit_store.log(event_type="thread_delete", actor_id="alice", outcome="success")

    rows = await audit_store.list(event_type="thread_delete")

    assert len(rows) == 1
    assert rows[0]["event_type"] == "thread_delete"


# ------------------------------------------------------------------ 输入校验


@pytest.mark.parametrize(
    "kwargs",
    [
        {"event_type": "", "actor_id": "a", "outcome": "success"},
        {"event_type": "e", "actor_id": "", "outcome": "success"},
        {"event_type": "e", "actor_id": "a", "outcome": ""},
    ],
)
async def test_log_rejects_blank_required_fields(audit_store, kwargs):
    with pytest.raises(ValueError):
        await audit_store.log(**kwargs)


@pytest.mark.parametrize("limit", [0, -1, 201])
async def test_list_rejects_bad_limit(audit_store, limit):
    with pytest.raises(ValueError):
        await audit_store.list(limit=limit)


async def test_list_rejects_negative_offset(audit_store):
    with pytest.raises(ValueError):
        await audit_store.list(offset=-1)


def test_constructor_rejects_none_conn():
    with pytest.raises(ValueError):
        AuditStore(None)


# ------------------------------------------------------------------ 保留策略


async def test_purge_expired_deletes_only_outdated_rows(audit_store):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=200)).isoformat(timespec="seconds")
    recent = (now - timedelta(days=10)).isoformat(timespec="seconds")

    await _insert_dated(audit_store, created_at=old)
    await _insert_dated(audit_store, created_at=recent, actor="bob")

    deleted = await audit_store.purge_expired(retention_days=180)

    assert deleted == 1
    remaining = await audit_store.list(limit=200)
    assert [row["actor_id"] for row in remaining] == ["bob"]


async def test_purge_expired_keeps_everything_when_nothing_expired(audit_store):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await _insert_dated(audit_store, created_at=now)

    assert await audit_store.purge_expired(retention_days=180) == 0
    assert await audit_store.count_all() == 1


async def test_purge_expired_boundary_is_exclusive(audit_store):
    """保留 7 天时，恰好 7 天前的记录仍应保留（边界按「早于」判定）。"""
    boundary = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    await _insert_dated(audit_store, created_at=boundary)

    assert await audit_store.purge_expired(retention_days=7) == 0
    assert await audit_store.count_all() == 1


@pytest.mark.parametrize("bad_days", [0, -1])
async def test_purge_expired_rejects_non_positive_days(audit_store, bad_days):
    with pytest.raises(ValueError):
        await audit_store.purge_expired(retention_days=bad_days)


async def test_purge_expired_rejects_non_integer_days(audit_store):
    with pytest.raises(ValueError):
        await audit_store.purge_expired(retention_days="7")  # type: ignore[arg-type]


async def test_count_all_counts_every_row(audit_store):
    await audit_store.log(event_type="a", actor_id="alice", outcome="success")
    await audit_store.log(event_type="b", actor_id="bob", outcome="failure")

    assert await audit_store.count_all() == 2
