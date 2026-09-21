"""附件存储的回归测试。

覆盖面：路径安全（会话 ID 不得含分隔符、附件 ID 形态）、磁盘名由 MIME 决定、
原始文件名净化、读写往返、损坏元数据跳过、内容索引、单个与整会话删除。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.attachments import (
    ATTACHMENTS_DIR,
    AttachmentError,
    attachment_dir,
    count_attachments,
    delete_attachment,
    delete_thread_attachments,
    extension_for_mime,
    index_by_sha256,
    list_attachments,
    load_attachment,
    sanitize_filename,
    save_attachment,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_THREAD = "a" * 32


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _save(root: Path, *, name: str = "shot.png", mime: str = "image/png", data: bytes = _PNG):
    return save_attachment(root, _THREAD, filename=name, mime_type=mime, data=data)


# ------------------------------------------------------------------ 纯函数


def test_extension_mapping_is_fixed():
    """扩展名由 MIME 决定，且不依赖宿主机的类型表。"""
    assert extension_for_mime("image/png") == "png"
    assert extension_for_mime("image/jpeg") == "jpg"
    assert extension_for_mime("IMAGE/WEBP; charset=binary") == "webp"


def test_extension_rejects_unknown_mime():
    with pytest.raises(AttachmentError, match="不支持"):
        extension_for_mime("application/pdf")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("photo.png", "photo.png"),
        ("C:\\Users\\me\\photo.png", "photo.png"),
        ("../../etc/passwd", "passwd"),
        ("\x00bad\x01name.png", "badname.png"),
        ("...", "unnamed"),
        ("", "unnamed"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_but_keeps_suffix():
    cleaned = sanitize_filename("x" * 500 + ".png")

    assert cleaned.endswith(".png")
    assert len(cleaned) <= 120


# ------------------------------------------------------------------ 路径安全


@pytest.mark.parametrize("thread_id", ["a/b", "a\\b", "..", "."])
def test_thread_id_with_separator_is_rejected(root: Path, thread_id):
    """会话 ID 在本层是**一个目录名**：含分隔符会把它变成多层目录。"""
    with pytest.raises(ValueError, match="分隔符|不能为空|路径"):
        attachment_dir(root, thread_id)


def test_attachment_dir_stays_inside_workspace(root: Path):
    directory = attachment_dir(root, _THREAD)

    assert directory.is_relative_to(root.resolve())
    assert directory.name == _THREAD
    assert directory.parent.name == ATTACHMENTS_DIR


@pytest.mark.parametrize("attachment_id", ["", "../../etc/passwd", "A" * 32, "0" * 31])
def test_load_rejects_malformed_attachment_id(root: Path, attachment_id):
    with pytest.raises(AttachmentError, match="ID 非法"):
        load_attachment(root, _THREAD, attachment_id)


# ------------------------------------------------------------------ 读写往返


def test_save_then_load_roundtrip(root: Path):
    record = _save(root, name="我的截图.png")

    assert record.filename == "我的截图.png"
    assert record.mime_type == "image/png"
    assert record.size == len(_PNG)
    assert record.path == f"/{ATTACHMENTS_DIR}/{_THREAD}/{record.id}.png"

    loaded, data = load_attachment(root, _THREAD, record.id)
    assert data == _PNG
    assert loaded.sha256 == record.sha256


def test_save_rejects_empty_content(root: Path):
    with pytest.raises(AttachmentError, match="不能为空"):
        save_attachment(root, _THREAD, filename="a.png", mime_type="image/png", data=b"")


def test_save_rejects_unsupported_mime(root: Path):
    with pytest.raises(AttachmentError, match="不支持"):
        save_attachment(root, _THREAD, filename="a.pdf", mime_type="application/pdf", data=_PNG)


def test_meta_is_written_after_blob(root: Path):
    """元数据后写：没有 meta 的孤儿 blob 不应被列举成可用附件。"""
    record = _save(root)
    directory = attachment_dir(root, _THREAD)

    (directory / f"{record.id}.json").unlink()

    assert list_attachments(root, _THREAD) == ()
    with pytest.raises(FileNotFoundError):
        load_attachment(root, _THREAD, record.id)


def test_load_missing_attachment_raises(root: Path):
    with pytest.raises(FileNotFoundError):
        load_attachment(root, _THREAD, "b" * 32)


# ------------------------------------------------------------------ 列举与索引


def test_list_sorted_and_counts(root: Path):
    first = _save(root)
    second = _save(root, name="b.png")

    records = list_attachments(root, _THREAD)

    # 排序键是上传时刻（界面按「先传的在前」展示），不是 ID 字典序
    assert [item.id for item in records] == [first.id, second.id]
    assert count_attachments(root, _THREAD) == 2
    assert list_attachments(root, "f" * 32) == ()


def test_corrupt_meta_is_skipped_not_fatal(root: Path):
    """一条坏记录不该让用户看不到同一会话下其余完好的附件。"""
    good = _save(root)
    (attachment_dir(root, _THREAD) / f"{'c' * 32}.json").write_text("{ not json", encoding="utf-8")

    records = list_attachments(root, _THREAD)

    assert [item.id for item in records] == [good.id]


def test_index_by_sha256_maps_content(root: Path):
    record = _save(root)

    index = index_by_sha256(root, _THREAD)

    assert index[record.sha256].id == record.id


# ------------------------------------------------------------------ 删除


def test_delete_attachment_is_idempotent(root: Path):
    record = _save(root)
    directory = attachment_dir(root, _THREAD)

    assert delete_attachment(root, _THREAD, record.id) is True
    assert list_attachments(root, _THREAD) == ()
    assert not (directory / f"{record.id}.json").exists()
    assert not (directory / f"{record.id}.png").exists()
    # 再删一次：返回 False 而不是抛错——重试与并发清理都会走这条路径
    assert delete_attachment(root, _THREAD, record.id) is False


def test_delete_thread_attachments_removes_directory(root: Path):
    _save(root)
    _save(root, name="second.png")
    # 孤儿 blob（模拟半成品写入）也应被一并清掉
    (attachment_dir(root, _THREAD) / ".deadbeef.png.tmp").write_bytes(b"x")

    removed = delete_thread_attachments(root, _THREAD)

    assert removed == 2
    assert not attachment_dir(root, _THREAD).exists()
    assert delete_thread_attachments(root, _THREAD) == 0


def test_delete_attachment_reports_damaged_meta_but_removes_file(root: Path):
    """元数据坏掉时仍要能删：留着它只会让列举里永远出现一条坏记录。"""
    record = _save(root)
    meta = attachment_dir(root, _THREAD) / f"{record.id}.json"
    meta.write_text(json.dumps({"id": record.id}), encoding="utf-8")

    assert delete_attachment(root, _THREAD, record.id) is True
    assert not meta.exists()
