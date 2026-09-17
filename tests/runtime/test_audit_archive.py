"""审计归档 sink 与「先归档后清理」链路的回归测试。

覆盖面：JSONL 写入与提交、空批次不留文件、写入失败不留半成品、
文件名冲突时的序号递增、清理任务在归档前后对数据库的影响，以及归档失败
时不删除（fail-closed）这一核心安全性质。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from runtime.audit_archive import AuditArchive, AuditArchiveBatch
from runtime.audit_retention import AuditRetentionWorker
from runtime.audit_store import AuditStore, expiry_cutoff


async def _seed(store: AuditStore, *, count: int, created_at: str) -> None:
    """写入若干审计事件并把时间改成给定值。"""
    for index in range(count):
        await store.log(
            event_type="thread.created",
            actor_id=f"user-{index}",
            outcome="success",
            details={"index": index},
        )

    # WHY 直接改库而不是伪造时钟：``log`` 用的是真实时间，只有落库后回拨
    # created_at 才能制造「已经超期」的记录，且不依赖任何时钟打桩。
    async with store._lock:
        await store._conn.execute("UPDATE audit_log SET created_at = ?", (created_at,))
        await store._conn.commit()


def _expired_at(days: int = 200) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


# ------------------------------------------------------------------ 归档写入


async def test_batch_writes_jsonl(tmp_path: Path):
    archive = AuditArchive(tmp_path / "archive")
    events = [
        {"id": 1, "event_type": "thread.created", "outcome": "success"},
        {"id": 2, "event_type": "run.started", "outcome": "success", "details": {"a": "中文"}},
    ]

    async with archive.batch() as batch:
        assert await batch.write(events) == 2

    files = list((tmp_path / "archive").glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["id"] for line in lines] == [1, 2]
    # 中文不得被转义成 \uXXXX：归档是要给人 grep 的
    assert "中文" in lines[1]
    # 提交后不应留下 .part 半成品
    assert list((tmp_path / "archive").glob("*.part")) == []


async def test_batch_without_events_creates_no_file(tmp_path: Path):
    archive = AuditArchive(tmp_path / "archive")

    async with archive.batch() as batch:
        assert await batch.write([]) == 0

    assert batch.count == 0
    assert not (tmp_path / "archive").exists()


async def test_batch_discards_partial_file_on_failure(tmp_path: Path):
    """WHY 本用例是「不留残缺归档」的契约：清理随后就会删库里的记录，
    若失败时把半成品留在归档目录里，运维会误以为数据已经安全落地。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("占位", encoding="utf-8")
    archive = AuditArchive(blocker / "archive")

    with pytest.raises(OSError):
        async with archive.batch() as batch:
            await batch.write([{"id": 1}])

    assert not (blocker / "archive").exists()
    assert blocker.is_file()


def test_build_path_increments_on_collision(tmp_path: Path):
    archive = AuditArchive(tmp_path, prefix="audit")
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    first = archive.build_path(moment)
    first.write_text("", encoding="utf-8")

    assert archive.build_path(moment) == tmp_path / "audit-20260102T030405Z-1.jsonl"


@pytest.mark.parametrize("prefix", ["", "   "])
def test_constructor_rejects_blank_prefix(tmp_path: Path, prefix: str):
    with pytest.raises(ValueError):
        AuditArchive(tmp_path, prefix=prefix)


def test_constructor_rejects_none_directory():
    with pytest.raises(ValueError):
        AuditArchive(None)


def test_batch_rejects_none_path():
    with pytest.raises(ValueError):
        AuditArchiveBatch(None)


# ------------------------------------------------------------------ 清理链路


async def test_prune_archives_before_deleting(audit_store: AuditStore, tmp_path: Path):
    await _seed(audit_store, count=5, created_at=_expired_at())
    worker = AuditRetentionWorker(
        audit_store,
        retention_days=30,
        interval_seconds=60,
        archive=AuditArchive(tmp_path / "archive"),
        batch_size=2,  # 故意小于总数，覆盖多批次读取
    )

    deleted = await worker.prune_once()

    assert deleted == 5
    assert await audit_store.count_all() == 0

    files = list((tmp_path / "archive").glob("*.jsonl"))
    assert len(files) == 1
    archived = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert len(archived) == 5
    # 归档内容必须完整保留业务字段，否则归档没有意义
    assert {item["event_type"] for item in archived} == {"thread.created"}
    assert all(item["outcome"] == "success" for item in archived)


async def test_prune_keeps_records_when_archive_fails(audit_store: AuditStore, tmp_path: Path):
    """WHY 本用例是本次改造的核心：归档失败时必须拦住删除，
    否则「清理成功」的假象会掩盖永久性的数据丢失。"""
    await _seed(audit_store, count=3, created_at=_expired_at())
    blocker = tmp_path / "blocked"
    blocker.write_text("占位", encoding="utf-8")
    worker = AuditRetentionWorker(
        audit_store,
        retention_days=30,
        interval_seconds=60,
        archive=AuditArchive(blocker / "archive"),
    )

    with pytest.raises(OSError):
        await worker.prune_once()

    assert await audit_store.count_all() == 3


async def test_prune_without_archive_deletes_directly(audit_store: AuditStore):
    await _seed(audit_store, count=2, created_at=_expired_at())
    worker = AuditRetentionWorker(audit_store, retention_days=30, interval_seconds=60)

    assert await worker.prune_once() == 2
    assert await audit_store.count_all() == 0


async def test_prune_leaves_fresh_records(audit_store: AuditStore, tmp_path: Path):
    await _seed(audit_store, count=2, created_at=_expired_at(days=1))
    worker = AuditRetentionWorker(
        audit_store,
        retention_days=30,
        interval_seconds=60,
        archive=AuditArchive(tmp_path / "archive"),
    )

    assert await worker.prune_once() == 0
    assert await audit_store.count_all() == 2
    assert not (tmp_path / "archive").exists()


# ------------------------------------------------------------------ 截止时间


async def test_purge_honors_explicit_cutoff(audit_store: AuditStore):
    """WHY 显式 cutoff 是归档与删除一致性的前提：传入更早的截止时间时，
    本该被删除的记录必须留下，否则就可能出现「扫过但没归档」的夹缝。"""
    await _seed(audit_store, count=2, created_at=_expired_at(days=50))

    assert await audit_store.purge_expired(retention_days=1, cutoff=_expired_at(days=100)) == 0
    assert await audit_store.count_all() == 2

    assert await audit_store.purge_expired(retention_days=1) == 2


def test_expiry_cutoff_rejects_bad_input():
    for invalid in (0, -1, "30", 1.5, None):
        with pytest.raises(ValueError):
            expiry_cutoff(invalid)


async def test_fetch_expired_paginates_by_id(audit_store: AuditStore):
    await _seed(audit_store, count=3, created_at=_expired_at())
    cutoff = expiry_cutoff(30)

    first = await audit_store.fetch_expired(cutoff=cutoff, limit=2)
    assert [row["id"] for row in first] == [1, 2]

    second = await audit_store.fetch_expired(cutoff=cutoff, limit=2, after_id=first[-1]["id"])
    assert [row["id"] for row in second] == [3]

    assert await audit_store.fetch_expired(cutoff=cutoff, after_id=second[-1]["id"]) == []


async def test_fetch_expired_ignores_fresh_records(audit_store: AuditStore):
    await _seed(audit_store, count=1, created_at=_expired_at(days=1))

    assert await audit_store.fetch_expired(cutoff=expiry_cutoff(30)) == []


@pytest.mark.parametrize("limit", [0, -1, 10_000, "10", None])
async def test_fetch_expired_rejects_bad_limit(audit_store: AuditStore, limit):
    with pytest.raises(ValueError):
        await audit_store.fetch_expired(cutoff=expiry_cutoff(30), limit=limit)


async def test_fetch_expired_rejects_bad_cutoff(audit_store: AuditStore):
    with pytest.raises(ValueError):
        await audit_store.fetch_expired(cutoff="")
    with pytest.raises(ValueError):
        await audit_store.fetch_expired(cutoff=expiry_cutoff(30), after_id=-1)


async def test_purge_expired_rejects_blank_cutoff(audit_store: AuditStore):
    with pytest.raises(ValueError):
        await audit_store.purge_expired(retention_days=30, cutoff="   ")
