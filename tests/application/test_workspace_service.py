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
from runtime.tool_outputs import tool_output_path, write_tool_output
from runtime.workspace_files import WorkspacePathError
from tests.conftest import make_config, make_root


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
    return WorkspaceService(config, scope=make_root(config), audit_store=audit), audit


def test_rejects_none_config():
    with pytest.raises(ValueError, match="config"):
        WorkspaceService(None, scope=None)  # type: ignore[arg-type]


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


async def test_read_file_pages_cover_the_whole_file(tmp_path: Path):
    """「完整查看」的机器判据：分页拼起来必须与文件逐字符一致。

    WHY 断言全等而不是逐页长度：单独看每一页都「返回了内容」，只有把它们拼起来
    与原文比对，才能发现漏页、重叠或某页被静默截短——那类缺陷在界面上表现为
    「内容看着挺多但就是缺一段」。"""
    service, _ = _service(tmp_path, workspace_file_preview_chars=100)

    chunks: list[str] = []
    offset = 0
    page = None
    for _ in range(200):  # 上限只为防死循环；真实页数由文件大小决定
        page = await service.read_file("/big.txt", offset=offset)
        assert page.offset == offset
        chunks.append(page.text)
        offset += len(page.text)
        if not page.truncated:
            break

    assert "".join(chunks) == "x" * 5000
    assert len(chunks) == 50  # 5000 字符 / 每页 100
    assert page is not None and page.truncated is False


async def test_read_file_rejects_negative_offset(tmp_path: Path):
    service, _ = _service(tmp_path)

    with pytest.raises(ValueError, match="offset"):
        await service.read_file("/big.txt", offset=-1)


async def test_read_file_past_end_returns_empty_tail(tmp_path: Path):
    service, _ = _service(tmp_path)

    page = await service.read_file("/big.txt", offset=99_999)

    assert page.text == ""
    assert page.truncated is False


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


# ------------------------------------------------------------------ 根外挂载点


async def test_reads_a_file_that_lives_in_a_mount(tmp_path: Path):
    """面板要能打开「完整输出」——它在**根外存储**里，经挂载暴露在 ``/_tool_outputs`` 下。

    WHY 单列：工具留存的正文只在这里，而它已经不在工作区里了。少了这一步，前端点开
    「完整输出」会拿到 404（看起来像留存没写成功），而文件其实好好躺在存储目录里。
    """
    audit = _StubAudit()
    config = make_config(tmp_path)
    scope = make_root(config)
    scope.ensure_storage()
    target = tool_output_path(scope.tool_output_store, "t1", 1, "execute")
    write_tool_output(target, "完整输出正文\n", max_chars=1000)
    service = WorkspaceService(config, scope=scope, audit_store=audit)

    content = await service.read_file("/_tool_outputs/t1/0001-execute.txt", Principal(user_id="u1"))

    assert content.kind == KIND_TEXT
    assert "完整输出正文" in (content.text or "")


async def test_list_dir_never_exposes_the_internal_stores(tmp_path: Path):
    """列举工作区根时不得出现那三个内部名字（技能库 / 技能视图 / 工具留存）。

    WHY 单列：它们现在是**挂载点**而不是根内目录，但这条约束与它们当年在根内时是同一个
    ——面板是给用户看「我的项目里有什么」的，程序产物混进去只会让人以为是自己建的。
    """
    service, _ = _service(tmp_path)

    listing = await service.list_dir("/", Principal(user_id="u1"))

    assert {item.name for item in listing.entries}.isdisjoint(
        {"skills", ".skills-active", "_tool_outputs"}
    )
