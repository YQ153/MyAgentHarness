"""附件服务的回归测试。

覆盖面：四项上限与白名单（大小 / MIME / 单会话张数 / ID 形态）、会话校验、
上传审计「有元信息无内容」、多模态消息构造（纯文本保持字符串、图片块、模型不
支持时显式拒绝）、清单里带上限、删除幂等，以及历史消息的附件回填。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from application.attachment_service import AttachmentService, attachment_info
from application.audit_context import LOCAL_ACTOR_ID
from application.errors import NotFoundError, VisionUnsupportedError
from application.thread_service import ThreadService
from llm.registry import build_default_registry
from runtime.attachments import AttachmentRecord, index_by_sha256, save_attachment
from runtime.thread_store import ThreadMetaStore, open_thread_store
from tests.conftest import StubSessionRegistry, make_config, make_root

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_THREAD = "a" * 32


class _StubAudit:
    """审计替身：记录调用，便于断言「落的是元信息、不是内容」。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def log(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture()
async def thread_store(tmp_path: Path) -> AsyncIterator[ThreadMetaStore]:
    async with open_thread_store(tmp_path / "threads.db") as store:
        yield store


def _service(tmp_path: Path, thread_store: ThreadMetaStore, **overrides: Any):
    config = make_config(tmp_path, **overrides)
    config.ensure_directories()
    audit = _StubAudit()
    registry = build_default_registry(config)
    service = AttachmentService(
        config,
        scope=make_root(config), registry=registry, thread_store=thread_store, audit_store=audit
    )
    return service, config, audit, registry


# ------------------------------------------------------------------ 上限与白名单


async def test_upload_rejects_oversized_content(tmp_path: Path, thread_store: ThreadMetaStore):
    service, _config, _audit, _registry = _service(
        tmp_path, thread_store, attachment_max_bytes=1024
    )

    with pytest.raises(ValueError, match="附件过大"):
        await service.upload(
            _THREAD, filename="big.png", mime_type="image/png", data=b"x" * 2000
        )


async def test_upload_rejects_disallowed_mime(tmp_path: Path, thread_store: ThreadMetaStore):
    service, _config, _audit, _registry = _service(tmp_path, thread_store)

    with pytest.raises(ValueError, match="不支持的文件类型"):
        await service.upload(
            _THREAD, filename="a.pdf", mime_type="application/pdf", data=_PNG
        )


async def test_upload_rejects_empty_content(tmp_path: Path, thread_store: ThreadMetaStore):
    service, _config, _audit, _registry = _service(tmp_path, thread_store)

    with pytest.raises(ValueError, match="不能为空"):
        await service.upload(_THREAD, filename="a.png", mime_type="image/png", data=b"")


async def test_upload_enforces_per_thread_quota(tmp_path: Path, thread_store: ThreadMetaStore):
    service, _config, _audit, _registry = _service(
        tmp_path, thread_store, attachment_max_per_thread=1
    )
    await service.upload(_THREAD, filename="a.png", mime_type="image/png", data=_PNG)

    with pytest.raises(ValueError, match="已达上限"):
        await service.upload(_THREAD, filename="b.png", mime_type="image/png", data=_PNG)


async def test_upload_rejects_thread_id_with_separator(
    tmp_path: Path, thread_store: ThreadMetaStore
):
    """会话 ID 会成为附件目录名：含分隔符必须被拒，而不是造出多层目录。"""
    service, _config, _audit, _registry = _service(tmp_path, thread_store)

    with pytest.raises(ValueError, match="分隔符"):
        await service.upload("a/b", filename="a.png", mime_type="image/png", data=_PNG)


# ------------------------------------------------------------------ 审计


async def test_upload_audits_metadata_without_content(
    tmp_path: Path, thread_store: ThreadMetaStore
):
    service, _config, audit, _registry = _service(tmp_path, thread_store)

    info = await service.upload(
        _THREAD, filename="shot.png", mime_type="image/png", data=_PNG
    )

    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["event_type"] == "attachment_upload"
    assert call["actor_id"] == LOCAL_ACTOR_ID
    assert call["action"] == "write"
    details = call["details"]
    assert details["sha256"] == info.sha256
    assert details["size"] == len(_PNG)
    assert details["mime_type"] == "image/png"
    # WHY 断言「不含内容」而不是只看字段名：审计表会被归档与人工检索，
    # 一旦有人把 base64 塞进 details，这条断言应当立刻转红。
    assert "content" not in details
    assert "data_url" not in details
    assert all("base64" not in str(value) for value in details.values())


# ------------------------------------------------------------------ 清单与删除


async def test_list_includes_limits(tmp_path: Path, thread_store: ThreadMetaStore):
    service, _config, _audit, _registry = _service(
        tmp_path, thread_store, attachment_max_bytes=2048
    )
    await thread_store.create(_THREAD)
    await service.upload(_THREAD, filename="a.png", mime_type="image/png", data=_PNG)

    result = await service.list(_THREAD)

    assert [item.filename for item in result.items] == ["a.png"]
    assert result.limits.max_bytes == 2048
    assert result.limits.max_per_thread == 8
    assert "image/png" in result.limits.allowed_mime_types


async def test_list_missing_thread_is_not_found(tmp_path: Path, thread_store: ThreadMetaStore):
    service, _config, _audit, _registry = _service(tmp_path, thread_store)

    with pytest.raises(NotFoundError):
        await service.list(_THREAD)


async def test_delete_is_idempotent(tmp_path: Path, thread_store: ThreadMetaStore):
    service, _config, _audit, _registry = _service(tmp_path, thread_store)
    await thread_store.create(_THREAD)
    info = await service.upload(_THREAD, filename="a.png", mime_type="image/png", data=_PNG)

    assert await service.delete(_THREAD, info.id) is True
    assert await service.delete(_THREAD, info.id) is False


# ------------------------------------------------------------ 多模态内容构造


async def test_build_user_content_keeps_plain_text_as_string(
    tmp_path: Path, thread_store: ThreadMetaStore
):
    """无附件时必须仍是字符串：改成统一列表会让所有历史消息的形状变化。"""
    service, _config, _audit, _registry = _service(tmp_path, thread_store)

    content = await service.build_user_content(_THREAD, "你好", [])

    assert content == "你好"


async def test_build_user_content_builds_image_block(
    tmp_path: Path, thread_store: ThreadMetaStore, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    service, _config, _audit, registry = _service(tmp_path, thread_store)
    info = await service.upload(_THREAD, filename="a.png", mime_type="image/png", data=_PNG)

    content = await service.build_user_content(
        _THREAD, "看图", [info.id], model_name="openai"
    )

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看图"}
    block = content[1]
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")
    assert registry.supports_vision("openai") is True


async def test_build_user_content_rejects_non_vision_model(
    tmp_path: Path, thread_store: ThreadMetaStore
):
    """模型不支持时必须显式报错并点名可用的模型，不能把图片静默丢掉。"""
    service, _config, _audit, _registry = _service(tmp_path, thread_store)
    info = await service.upload(_THREAD, filename="a.png", mime_type="image/png", data=_PNG)

    with pytest.raises(VisionUnsupportedError) as excinfo:
        await service.build_user_content(
            _THREAD, "看图", [info.id], model_name="deepseek-flash"
        )

    assert "deepseek-flash" in str(excinfo.value)
    assert excinfo.value.supported == []


async def test_build_user_content_missing_attachment_is_not_found(
    tmp_path: Path, thread_store: ThreadMetaStore, monkeypatch: pytest.MonkeyPatch
):
    """引用一个不存在的附件 → 404。

    WHY 这里显式切到多模态模型：能力判定**排在**附件存在性之前（它更便宜、也更根本
    ——换模型是用户唯一可做的动作），因此用不支持图片的模型测这条分支会被前一个
    判定先拦下。
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    service, _config, _audit, _registry = _service(tmp_path, thread_store)

    with pytest.raises(NotFoundError, match="附件"):
        await service.build_user_content(_THREAD, "看图", ["c" * 32], model_name="openai")


async def test_build_user_content_rejects_malformed_ids(
    tmp_path: Path, thread_store: ThreadMetaStore
):
    service, _config, _audit, _registry = _service(tmp_path, thread_store)

    with pytest.raises(ValueError, match="非空字符串"):
        await service.build_user_content(_THREAD, "看图", [""], model_name="deepseek-flash")


# ------------------------------------------------------------ 历史消息回填


def _record(tmp_path: Path, thread_id: str = _THREAD) -> AttachmentRecord:
    config = make_config(tmp_path)
    config.ensure_directories()
    return save_attachment(
        Path(make_root(config).root), thread_id, filename="shot.png", mime_type="image/png", data=_PNG
    )


def test_history_message_extracts_attachment_from_image_block(tmp_path: Path):
    record = _record(tmp_path)
    index = {record.sha256: record}
    message = HumanMessage(
        content=[
            {"type": "text", "text": "看图"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
                },
            },
        ]
    )

    dto = ThreadService._message_to_dto(message, index)  # noqa: SLF001 被测对象就是它

    assert dto.content == "看图"
    assert [item.id for item in dto.attachments] == [record.id]


def test_history_message_never_leaks_base64_into_content(tmp_path: Path):
    """列表型 content 若被 ``str()`` 转换，几 MB 的 base64 会进历史响应。"""
    message = HumanMessage(
        content=[
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]
    )

    dto = ThreadService._message_to_dto(message, {})  # noqa: SLF001

    assert dto.content == "看图"
    assert "base64" not in dto.content
    assert dto.attachments == []


def test_history_message_unmatched_image_is_skipped(tmp_path: Path):
    """附件已被删除时，历史里的图片块只略过，不让整次读取失败。"""
    message = HumanMessage(
        content=[
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]
    )

    dto = ThreadService._message_to_dto(message, {"deadbeef": _record(tmp_path)})  # noqa: SLF001

    assert dto.attachments == []


def test_attachment_info_maps_all_fields(tmp_path: Path):
    record = _record(tmp_path)

    info = attachment_info(record)

    assert info.thread_id == record.thread_id
    assert info.path.endswith(".png")
    assert info.size == len(_PNG)


def test_index_by_sha256_finds_saved_attachment(tmp_path: Path):
    record = _record(tmp_path)
    config = make_config(tmp_path)

    assert index_by_sha256(Path(make_root(config).root), _THREAD)[record.sha256].id == record.id
