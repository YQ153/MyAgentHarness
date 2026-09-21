"""长期记忆管理服务的测试。

覆盖面：清单与删除的读写口径、路径归一、命名空间收敛、上限与截断，以及
「删除落审计、审计失败不影响删除、存储失败映射为 RuntimeError」。
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from agent.run_context import ANONYMOUS_USER_ID, memory_namespace
from application.memory_service import (
    _MAX_CONTENT_CHARS,
    _MAX_ITEMS,
    MemoryService,
    to_store_key,
    to_virtual_path,
)
from tests.application.test_run_governance import RecordingAuditStore
from tests.conftest import make_config


class BrokenStore(InMemoryStore):
    """所有读写都失败的存储替身。"""

    def __init__(self, *, fail_read: bool = True, fail_delete: bool = False) -> None:
        super().__init__()
        self._fail_read = fail_read
        self._fail_delete = fail_delete

    async def asearch(self, *args: Any, **kwargs: Any) -> Any:
        if self._fail_read:
            raise OSError("数据库文件损坏")
        return await super().asearch(*args, **kwargs)

    async def aget(self, *args: Any, **kwargs: Any) -> Any:
        if self._fail_read:
            raise OSError("数据库文件损坏")
        return await super().aget(*args, **kwargs)

    async def adelete(self, *args: Any, **kwargs: Any) -> Any:
        if self._fail_delete:
            raise OSError("数据库文件损坏")
        return await super().adelete(*args, **kwargs)


async def _seed(
    store: InMemoryStore,
    owner: str,
    key: str,
    content: str,
    **extra: Any,
) -> None:
    """按命名空间写入一条记忆。"""
    await store.aput(
        memory_namespace(owner),
        key,
        {"content": content, "encoding": "utf-8", **extra},
    )


def _service(
    tmp_path,
    store: InMemoryStore | None = None,
    *,
    audit: RecordingAuditStore | None = None,
) -> MemoryService:
    config = make_config(tmp_path)
    return MemoryService(
        config,
        store=store if store is not None else InMemoryStore(),
        audit_store=audit if audit is not None else RecordingAuditStore(),
    )


# ------------------------------------------------------------------ 路径归一


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/memories/notes.md", "/notes.md"),
        ("/notes.md", "/notes.md"),
        ("notes.md", "/notes.md"),
        ("memories/notes.md", "/notes.md"),
        ("  /memories/sub/deep.md  ", "/sub/deep.md"),
        ("/memories/sub\\deep.md", "/sub/deep.md"),
    ],
)
def test_to_store_key_accepts_both_forms(raw, expected):
    """GET 返回的完整虚拟路径与存储层裸键都要能收：前端直接拼 path 即可。"""
    assert to_store_key(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "/memories/", "/memories", None, 123, "/memories/a/", "/memories/../x", "/a/b/../c"],
)
def test_to_store_key_rejects_invalid(raw):
    with pytest.raises(ValueError):
        to_store_key(raw)


def test_to_virtual_path_round_trips():
    assert to_virtual_path(to_store_key("/memories/notes.md")) == "/memories/notes.md"
    assert to_virtual_path("/memories/notes.md") == "/memories/notes.md"
    assert to_virtual_path("notes.md") == "/memories/notes.md"


# ------------------------------------------------------------------ 构造校验


def test_constructor_rejects_none_deps(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(ValueError):
        MemoryService(None, store=InMemoryStore())
    with pytest.raises(ValueError):
        MemoryService(config, store=None)


# ------------------------------------------------------------------ 清单


async def test_list_returns_empty_for_new_owner(tmp_path):
    result = await _service(tmp_path).list_memories()

    assert result.items == []
    assert result.total == 0
    assert result.truncated is False
    assert result.owner_id == ANONYMOUS_USER_ID


async def test_list_maps_store_items_to_virtual_paths(tmp_path):
    store = InMemoryStore()
    await _seed(store, ANONYMOUS_USER_ID, "/b.md", "第二条", modified_at="2026-01-02T00:00:00+00:00")
    await _seed(store, ANONYMOUS_USER_ID, "/a.md", "第一条", created_at="2026-01-01T00:00:00+00:00")

    result = await _service(tmp_path, store).list_memories()

    assert [item.path for item in result.items] == ["/memories/a.md", "/memories/b.md"]
    assert [item.content for item in result.items] == ["第一条", "第二条"]
    assert result.items[0].created_at == "2026-01-01T00:00:00+00:00"
    assert result.items[1].updated_at == "2026-01-02T00:00:00+00:00"


async def test_list_joins_legacy_line_list_content(tmp_path):
    """旧版把正文存成行列表；读不出来会让整个面板空白，必须容忍。"""
    store = InMemoryStore()
    await store.aput(memory_namespace(ANONYMOUS_USER_ID), "/old.md", {"content": ["第一行", "第二行"]})

    result = await _service(tmp_path, store).list_memories()

    assert result.items[0].content == "第一行\n第二行"


async def test_list_truncates_content_and_flags_it(tmp_path):
    store = InMemoryStore()
    long_text = "字" * (_MAX_CONTENT_CHARS + 50)
    await _seed(store, ANONYMOUS_USER_ID, "/big.md", long_text)

    result = await _service(tmp_path, store).list_memories()

    assert result.items[0].truncated is True
    assert result.truncated is True
    assert len(result.items[0].content) < len(long_text)
    assert result.items[0].content.startswith("字" * 10)


async def test_list_caps_items_and_flags_truncation(tmp_path):
    """超上限必须显式告知：否则「被截断的清单」与「记忆本来就少」无法区分。"""
    store = InMemoryStore()
    for index in range(_MAX_ITEMS + 1):
        await _seed(store, ANONYMOUS_USER_ID, f"/m{index:04d}.md", "内容")

    result = await _service(tmp_path, store).list_memories()

    assert result.total == _MAX_ITEMS
    assert result.truncated is True


async def test_list_failure_is_runtime_error(tmp_path):
    with pytest.raises(RuntimeError, match="读取长期记忆失败"):
        await _service(tmp_path, BrokenStore()).list_memories()


# ------------------------------------------------------------------ 命名空间


async def test_list_only_reads_the_local_namespace(tmp_path):
    """只读本机命名空间：别的命名空间里写得再多，也不会串进这份清单。"""
    store = InMemoryStore()
    await _seed(store, ANONYMOUS_USER_ID, "/a.md", "本机记忆")
    await _seed(store, "someone-else", "/b.md", "别人的记忆")
    service = _service(tmp_path, store)

    result = await service.list_memories()

    assert [item.content for item in result.items] == ["本机记忆"]


async def test_disabled_mode_uses_anonymous_pool(tmp_path):
    store = InMemoryStore()
    await _seed(store, ANONYMOUS_USER_ID, "/a.md", "本地记忆")
    service = _service(tmp_path, store)

    result = await service.list_memories()

    assert [item.content for item in result.items] == ["本地记忆"]


# ------------------------------------------------------------------ 删除


async def test_delete_removes_item_and_audits(tmp_path):
    store = InMemoryStore()
    audit = RecordingAuditStore()
    await _seed(store, ANONYMOUS_USER_ID, "/a.md", "待删除")
    service = _service(tmp_path, store, audit=audit)

    result = await service.delete_memory("/memories/a.md")

    assert result.deleted is True
    assert result.path == "/memories/a.md"
    assert await store.asearch(memory_namespace(ANONYMOUS_USER_ID)) == []
    events = audit.of_type("memory_delete")
    assert len(events) == 1
    assert events[0]["target_id"] == "/memories/a.md"
    assert events[0]["outcome"] == "success"


async def test_delete_missing_item_is_idempotent(tmp_path):
    """幂等：界面刷新后重放请求不该变成一次报错，也不该污染审计。"""
    audit = RecordingAuditStore()
    service = _service(tmp_path, InMemoryStore(), audit=audit)

    result = await service.delete_memory("/memories/ghost.md")

    assert result.deleted is False
    assert audit.of_type("memory_delete") == []


async def test_delete_rejects_invalid_path(tmp_path):
    with pytest.raises(ValueError):
        await _service(tmp_path).delete_memory("/memories/")


async def test_delete_failure_is_runtime_error(tmp_path):
    store = BrokenStore(fail_read=False, fail_delete=True)
    await _seed(store, ANONYMOUS_USER_ID, "/a.md", "待删除")
    service = _service(tmp_path, store)

    with pytest.raises(RuntimeError, match="删除长期记忆失败"):
        await service.delete_memory("/memories/a.md")


async def test_delete_survives_audit_failure(tmp_path):
    """审计是旁路职责：审计库挂了不能让「删除记忆」这个动作失败。"""
    class FailingAudit(RecordingAuditStore):
        async def log(self, **kwargs: Any) -> None:
            raise RuntimeError("审计库不可用")

    store = InMemoryStore()
    await _seed(store, ANONYMOUS_USER_ID, "/a.md", "待删除")
    service = _service(tmp_path, store, audit=FailingAudit())

    result = await service.delete_memory("/memories/a.md")

    assert result.deleted is True
    assert await store.asearch(memory_namespace(ANONYMOUS_USER_ID)) == []
