"""工作区文件服务的回归测试。

覆盖面：目录列举（目录在前、截断标记、不存在）、文件分类（文本 / 图片 / 二进制 /
超限）、预览截断、读取落审计而列举不落审计、逃逸路径上抛。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from application.errors import NotFoundError
from application.principal import Principal
from application.workspace_service import (
    KIND_BINARY,
    KIND_IMAGE,
    KIND_TEXT,
    KIND_TOO_LARGE,
    WorkspaceService,
)
from runtime.workspace_files import WorkspacePathError
from tests.conftest import make_config


class _StubAudit:
    """审计存储替身：记录每次调用，便于断言「谁读了什么」以及「列举不落审计」。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def log(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def target_ids(self) -> list[Any]:
        return [call.get("target_id") for call in self.calls]


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path) -> Path:
    """在 ``make_config`` 约定的位置建一个真实工作区，内含四种典型文件。

    WHY 自动生效：服务从配置里取工作区根，而不是从用例参数取——夹具若只在被显式
    请求时才建目录，用例会拿到一个「路径对但不存在」的根，失败信息还长得像业务
    缺陷（``目录不存在：/``）。
    """
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "big.txt").write_text("x" * 5000, encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02")
    (root / "pixel.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    return root


def _service(tmp_path: Path, **overrides: Any) -> tuple[WorkspaceService, _StubAudit]:
    audit = _StubAudit()
    config = make_config(tmp_path, **overrides)
    return WorkspaceService(config, audit_store=audit), audit


def test_rejects_none_config():
    with pytest.raises(ValueError, match="config"):
        WorkspaceService(None)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 列目录


async def test_list_dir_reports_entries_and_skips_audit(tmp_path: Path):
    service, audit = _service(tmp_path)

    listing = await service.list_dir("/", Principal(user_id="u1"))

    assert listing.path == "/"
    assert listing.parent is None
    assert [item.name for item in listing.entries] == ["src", "big.txt", "blob.bin", "pixel.png"]
    assert listing.entries[0].is_dir is True
    assert listing.entries[1].size == 5000
    # 列举是高频动作且不暴露内容：落审计只会把审计表刷成噪声
    assert audit.calls == []


async def test_list_dir_marks_truncation(tmp_path: Path):
    service, _ = _service(tmp_path, workspace_list_max_entries=2)

    listing = await service.list_dir("/")

    assert len(listing.entries) == 2
    assert listing.truncated is True


async def test_list_dir_reports_missing_directory(tmp_path: Path):
    service, _ = _service(tmp_path)

    with pytest.raises(NotFoundError):
        await service.list_dir("/nope")


async def test_list_dir_rejects_escaping_path(tmp_path: Path):
    service, _ = _service(tmp_path)

    with pytest.raises(WorkspacePathError):
        await service.list_dir("/../outside")


# ------------------------------------------------------------------ 读文件


async def test_read_text_file(tmp_path: Path):
    service, audit = _service(tmp_path)

    content = await service.read_file("/src/app.py", Principal(user_id="u1"))

    assert content.kind == KIND_TEXT
    assert "print('hi')" in content.text
    assert content.truncated is False
    # WHY 与磁盘上的真实大小比较而不是字面量长度：Windows 的文本模式写入会把
    # ``\n`` 转成 ``\r\n``，按字面量断言会在别人机器上以「size 差 1」的形式失败。
    assert content.size == (tmp_path / "workspace" / "src" / "app.py").stat().st_size
    assert content.mime_type.startswith("text/")
    # 「谁读了哪个文件」正是审计要回答的问题——读取必须留痕
    assert audit.target_ids() == ["/src/app.py"]
    assert audit.calls[0]["event_type"] == "file_read"
    assert audit.calls[0]["actor_id"] == "u1"
    # 正文绝不进审计：审计表不是内容仓库
    assert "print" not in str(audit.calls[0])


async def test_read_file_truncates_preview(tmp_path: Path):
    service, _ = _service(tmp_path, workspace_file_preview_chars=100)

    content = await service.read_file("/big.txt")

    assert content.kind == KIND_TEXT
    assert content.truncated is True
    assert len(content.text) == 100
    # size 仍是文件真实大小：界面要能显示「5000 字节，仅预览前 100 字符」
    assert content.size == 5000


async def test_read_file_classifies_binary(tmp_path: Path):
    service, _ = _service(tmp_path)

    content = await service.read_file("/blob.bin")

    assert content.kind == KIND_BINARY
    assert content.text == ""


async def test_read_file_returns_image_as_data_url(tmp_path: Path):
    """WHY 断言这一支：PNG 字节里必然含空字节，若二进制判定排在图片判定之前，
    图片永远走不到这里，界面上就只剩一句「无法预览」。"""
    service, _ = _service(tmp_path)

    content = await service.read_file("/pixel.png")

    assert content.kind == KIND_IMAGE
    assert content.text.startswith("data:image/png;base64,")
    assert content.mime_type == "image/png"


async def test_read_file_marks_oversized_file(tmp_path: Path):
    service, audit = _service(tmp_path, workspace_file_max_bytes=1024)

    content = await service.read_file("/big.txt")

    assert content.kind == KIND_TOO_LARGE
    assert content.text == ""
    assert content.size == 5000
    # 超限也要留痕：读取动作已经发生，只是没有回传正文
    assert audit.target_ids() == ["/big.txt"]


async def test_read_file_reports_missing_and_directory(tmp_path: Path):
    service, _ = _service(tmp_path)

    with pytest.raises(NotFoundError):
        await service.read_file("/nope.txt")
    with pytest.raises(NotFoundError):
        await service.read_file("/src")


async def test_read_file_rejects_escaping_path(tmp_path: Path):
    service, _ = _service(tmp_path)

    with pytest.raises(WorkspacePathError):
        await service.read_file("/../outside.txt")
