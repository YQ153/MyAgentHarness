"""会话服务：重命名、归档与清单过滤的测试。

WHY 单独成文件：这三件事都在「整理自己的会话清单」这条线上，
与运行期事件流（``RunService``）、审计来源富化是不同关注点；混在一起会让
用例的意图被文件标题掩盖。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest

from application.dto import DeleteOutcome
from application.errors import NotFoundError
from application.thread_service import ThreadService
from runtime.attachments import attachment_dir, save_attachment
from runtime.thread_store import ThreadMetaStore
from tests.application.test_audit_enrichment import FakeCheckpointer, FakeGraphFactory
from tests.application.test_run_governance import RecordingAuditStore
from tests.application.test_session_registry import _registry
from tests.conftest import StubSessionRegistry, make_config, make_root


class _HistoryGraph:
    """只实现 ``aget_state`` 的图替身：历史读取只需要这一个方法。"""

    async def aget_state(self, config: Any) -> Any:
        del config
        return SimpleNamespace(values={"messages": []})


class _RootGuardedGraphFactory:
    """像 ``build_backend`` 一样要求「根必须是已存在的目录」的图工厂替身。

    WHY 专门造这个替身而不是用 ``FakeGraphFactory``：那条断言正是本次 bug 的爆点——
    ``FakeGraphFactory`` 不碰根，于是「根还不存在」这件事在用例里根本不会暴露，
    而真实装配里它是**在构造 Backend 的第一步**就检查的。
    """

    def __init__(self) -> None:
        self.scopes: list[Any] = []

    def get(self, name: str | None = None, *, scope: Any = None) -> Any:
        del name
        self.scopes.append(scope)
        if not scope.root.is_dir():
            raise NotADirectoryError(f"文件根不是一个有效目录：{scope.root}")
        return _HistoryGraph()


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
        workspaces=StubSessionRegistry(config),
        audit_store=audit if audit is not None else RecordingAuditStore(),
    )


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


async def test_list_includes_archived_when_requested(tmp_path, thread_store):
    """归档只是软删除：勾了「含已归档」就必须能看到它。"""
    service = _service(tmp_path, thread_store)
    await thread_store.record_turn("a1", turn_delta=1)
    await service.set_archived("a1", True)

    listed = await service.list_threads(include_archived=True)

    assert [item.thread_id for item in listed.items] == ["a1"]


# ------------------------------------------------------------------ 删除与附件


async def test_delete_removes_attachments(tmp_path, thread_store):
    """会话删除必须连带清掉附件目录。

    WHY 单列一条：附件是工作区里的真实文件，而会话一旦删除，它们不再被任何消息
    引用、界面上也没有入口能看到——留下的每一个都是纯泄漏，且不会有人发现。
    """
    config = make_config(tmp_path)
    config.ensure_directories()
    workspace = Path(make_root(config).root)
    service = _service(tmp_path, thread_store)
    # WHY 要显式绑定：不绑定工作空间的会话落在**它自己的专属目录**里，而附件是写在
    # 会话根下的——不绑定就会去清理另一个目录，本用例要验的「删会话连带删附件」也就
    # 变成了空转。
    await thread_store.create("a" * 32, workspace=str(workspace), workspace_bound=True)
    save_attachment(
        workspace, "a" * 32, filename="shot.png", mime_type="image/png", data=b"\x89PNG\r\n"
    )
    assert attachment_dir(workspace, "a" * 32).is_dir()

    result = await service.delete_thread("a" * 32)

    # 结果分类取决于 checkpointer 是否支持删除（本用例的替身不支持 → PARTIAL），
    # 而附件清理与它无关：会话记录一旦删掉，附件就必须一并消失。
    assert result.outcome in (DeleteOutcome.DELETED, DeleteOutcome.PARTIAL)
    assert not attachment_dir(workspace, "a" * 32).exists()


# ------------------------------------------------------------------ 历史


async def test_history_opens_a_session_whose_directory_does_not_exist_yet(
    tmp_path, thread_store
):
    """打开一条「没选工作空间、还没跑过」的会话不该报错（回归）。

    WHY 单列：这条会话的根是应用按会话 ID 派生的**专属目录**，而它在第一次真正需要落文件
    之前并不存在。读历史这条路径不经过服务装配（它只要图与附件索引），于是它会拿着一个
    不存在的根走到 Backend 里那条「根必须是已存在的目录」的断言上——用户点开侧栏里自己
    的会话，看到的是 HTTP 500。

    修复的位置是解析本身：``SessionRegistry.resolve`` 保证交出来的根一定可用（专属目录
    按需创建）。本用例用真注册表 + 复刻那条断言的图工厂替身，因此它验的正是「解析的结果
    经得起下游的前提」，而不是某个具体调用点又补了一次。
    """
    config = make_config(tmp_path)
    registry = _registry(config, thread_store)
    factory = _RootGuardedGraphFactory()
    service = ThreadService(
        config,
        checkpointer=FakeCheckpointer(),
        thread_store=thread_store,
        graph_factory=factory,
        workspaces=registry,
    )
    await thread_store.create("t1")

    assert not config.session_dir("t1").exists(), "前提：专属目录此时还不存在"

    messages = await service.history("t1")

    assert messages == []
    assert factory.scopes[0].root == config.session_dir("t1")
    assert config.session_dir("t1").is_dir(), "解析时就该把专属目录建出来"
