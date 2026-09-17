"""会话服务：重命名、归档与清单过滤的测试。

WHY 单独成文件：这三件事都在「所有者对自己会话清单的处置权」这条线上，
与运行期事件流（``RunService``）、审计来源富化是不同关注点；混在一起会让
用例的意图被文件标题掩盖。
"""

from __future__ import annotations

from typing import Any

import aiosqlite
import pytest

from application.errors import NotFoundError, OwnershipError
from application.principal import Principal
from application.thread_service import ThreadService
from runtime.thread_store import ThreadMetaStore
from tests.application.test_audit_enrichment import FakeCheckpointer, FakeGraphFactory
from tests.application.test_run_governance import RecordingAuditStore
from tests.conftest import make_config


class FailingRenameStore(ThreadMetaStore):
    """``rename`` 必定失败的存储替身（其余方法沿用真实实现）。"""

    async def rename(self, thread_id: str, title: str) -> dict[str, Any] | None:
        raise aiosqlite.OperationalError("database disk image is malformed")


class FailingArchiveStore(ThreadMetaStore):
    """``set_archived`` 必定失败的存储替身。"""

    async def set_archived(self, thread_id: str, archived: bool) -> dict[str, Any] | None:
        raise aiosqlite.OperationalError("database disk image is malformed")


def _service(
    tmp_path,
    store: ThreadMetaStore,
    *,
    audit: RecordingAuditStore | None = None,
    **overrides: Any,
) -> ThreadService:
    config = make_config(tmp_path, **overrides)
    return ThreadService(
        config,
        checkpointer=FakeCheckpointer(),
        thread_store=store,
        graph_factory=FakeGraphFactory(),
        audit_store=audit if audit is not None else RecordingAuditStore(),
    )


def _principal(user_id: str, role: str = "member") -> Principal:
    return Principal(user_id=user_id, role=role)


# ------------------------------------------------------------------ 重命名


async def test_rename_updates_title_and_audits(tmp_path, thread_store):
    audit = RecordingAuditStore()
    service = _service(tmp_path, thread_store, audit=audit)
    await thread_store.record_turn("t1", title_hint="帮我看看这个", turn_delta=1)

    result = await service.rename_thread("t1", "  排查登录超时  ")

    assert result.thread_id == "t1"
    assert result.title == "排查登录超时"
    assert (await thread_store.get("t1"))["title"] == "排查登录超时"

    events = audit.of_type("thread_rename")
    assert len(events) == 1
    assert events[0]["target_id"] == "t1"
    assert events[0]["details"]["title"] == "排查登录超时"


@pytest.mark.parametrize("title", ["", "   ", "\n\t"])
async def test_rename_rejects_blank_title(tmp_path, thread_store, title):
    """空标题会让会话在清单里变成一行无字条目，用户会以为会话丢了。"""
    service = _service(tmp_path, thread_store)
    await thread_store.create("t1", title="旧")

    with pytest.raises(ValueError, match="不能为空"):
        await service.rename_thread("t1", title)


async def test_rename_rejects_non_string_title(tmp_path, thread_store):
    service = _service(tmp_path, thread_store)
    await thread_store.create("t1", title="旧")

    with pytest.raises(ValueError, match="字符串"):
        await service.rename_thread("t1", 123)


async def test_rename_respects_configured_limit(tmp_path, thread_store):
    """上限走配置：超长直接报错而不是截断，避免用户输入被静默改写。"""
    service = _service(tmp_path, thread_store, thread_rename_max_chars=10)
    await thread_store.create("t1", title="旧")

    assert (await service.rename_thread("t1", "x" * 10)).title == "x" * 10
    with pytest.raises(ValueError, match="过长"):
        await service.rename_thread("t1", "x" * 11)


async def test_rename_missing_thread_is_not_found(tmp_path, thread_store):
    service = _service(tmp_path, thread_store)

    with pytest.raises(NotFoundError):
        await service.rename_thread("ghost", "新标题")


async def test_rename_foreign_thread_is_denied(tmp_path, thread_store):
    service = _service(tmp_path, thread_store, auth_mode="apikey", auth_session_secret="s" * 32)
    await thread_store.record_turn("t1", title_hint="alice 的会话", turn_delta=1, owner_id="alice")

    with pytest.raises(OwnershipError):
        await service.rename_thread("t1", "bob 改名", _principal("bob"))


async def test_admin_can_rename_others_thread(tmp_path, thread_store):
    """管理员是运维兜底路径：与「删他人会话」保持同一口径。"""
    service = _service(tmp_path, thread_store, auth_mode="apikey", auth_session_secret="s" * 32)
    await thread_store.record_turn("t1", title_hint="alice 的会话", turn_delta=1, owner_id="alice")

    result = await service.rename_thread("t1", "admin 改名", _principal("root", role="admin"))

    assert result.title == "admin 改名"


async def test_rename_storage_failure_is_runtime_error(tmp_path, thread_store):
    service = _service(tmp_path, FailingRenameStore(thread_store._conn))
    await thread_store.create("t1", title="旧")

    with pytest.raises(RuntimeError, match="重命名会话失败"):
        await service.rename_thread("t1", "新标题")


# ------------------------------------------------------------------ 归档


async def test_archive_and_restore_audits_both(tmp_path, thread_store):
    audit = RecordingAuditStore()
    service = _service(tmp_path, thread_store, audit=audit)
    await thread_store.create("t1", title="会话")

    archived = await service.set_archived("t1", True)
    restored = await service.set_archived("t1", False)

    assert archived.archived is True
    assert archived.archived_at
    assert restored.archived is False

    events = audit.of_type("thread_archive")
    assert [event["action"] for event in events] == ["archive", "unarchive"]
    assert [event["details"]["archived"] for event in events] == [True, False]


async def test_archive_rejects_non_bool(tmp_path, thread_store):
    service = _service(tmp_path, thread_store)
    await thread_store.create("t1")

    with pytest.raises(ValueError, match="archived"):
        await service.set_archived("t1", "yes")


async def test_archive_missing_thread_is_not_found(tmp_path, thread_store):
    service = _service(tmp_path, thread_store)

    with pytest.raises(NotFoundError):
        await service.set_archived("ghost", True)


async def test_archive_foreign_thread_is_denied(tmp_path, thread_store):
    service = _service(tmp_path, thread_store, auth_mode="apikey", auth_session_secret="s" * 32)
    await thread_store.record_turn("t1", turn_delta=1, owner_id="alice")

    with pytest.raises(OwnershipError):
        await service.set_archived("t1", True, _principal("bob"))


async def test_archive_storage_failure_is_runtime_error(tmp_path, thread_store):
    service = _service(tmp_path, FailingArchiveStore(thread_store._conn))
    await thread_store.create("t1")

    with pytest.raises(RuntimeError, match="归档状态失败"):
        await service.set_archived("t1", True)


# ------------------------------------------------------------------ 清单过滤


async def test_list_excludes_archived_by_default(tmp_path, thread_store):
    service = _service(tmp_path, thread_store)
    await thread_store.create("keep", title="保留")
    await thread_store.create("hidden", title="收起")
    await service.set_archived("hidden", True)

    default = await service.list_threads()
    with_archived = await service.list_threads(include_archived=True)

    assert [item.thread_id for item in default.items] == ["keep"]
    assert default.total == 1
    assert sorted(item.thread_id for item in with_archived.items) == ["hidden", "keep"]
    assert with_archived.total == 2


async def test_list_filters_by_query_and_keeps_total_consistent(tmp_path, thread_store):
    """总数必须与实际返回条数同口径，否则前端会显示翻不到的「下一页」。"""
    service = _service(tmp_path, thread_store)
    await thread_store.create("t1", title="排查登录超时")
    await thread_store.create("t2", title="写周报")

    result = await service.list_threads(query="登录")

    assert [item.thread_id for item in result.items] == ["t1"]
    assert result.total == len(result.items) == 1


async def test_list_exposes_archive_fields(tmp_path, thread_store):
    service = _service(tmp_path, thread_store)
    await thread_store.create("t1", title="会话")
    await service.set_archived("t1", True)

    item = (await service.list_threads(include_archived=True)).items[0]

    assert item.archived is True
    assert item.archived_at


async def test_list_rejects_invalid_query(tmp_path, thread_store):
    """参数错误必须原样透出为 ValueError：路由层要靠它映射 400 而不是 500。"""
    service = _service(tmp_path, thread_store)

    with pytest.raises(ValueError, match="query 过长"):
        await service.list_threads(query="x" * 201)


async def test_list_archived_scope_respects_owner(tmp_path, thread_store):
    """归档不改变归属：member 勾了「含已归档」也只看得到自己的。"""
    service = _service(tmp_path, thread_store, auth_mode="apikey", auth_session_secret="s" * 32)
    await thread_store.record_turn("a1", turn_delta=1, owner_id="alice")
    await thread_store.record_turn("b1", turn_delta=1, owner_id="bob")
    await service.set_archived("a1", True, _principal("alice"))

    alice = await service.list_threads(_principal("alice"), include_archived=True)
    bob = await service.list_threads(_principal("bob"), include_archived=True)

    assert [item.thread_id for item in alice.items] == ["a1"]
    assert [item.thread_id for item in bob.items] == ["b1"]
