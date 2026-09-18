"""工作区路径校验与文件访问的回归测试。

WHY 重点放在「逃逸」而不是「正常读取」：正常路径读错会立刻被发现，而逃逸是
**静默放行**——放行一次越界读取不会报错、不会留痕，只会在某个时刻被人利用。
安全边界必须由逐条用例钉住已知绕过路径。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from runtime.workspace_files import (
    WorkspacePathError,
    list_directory,
    looks_binary,
    read_bytes_capped,
    resolve_in_workspace,
    to_virtual_path,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """一个照真实形态搭的工作区：有子目录、源码文件与根级文档。"""
    root = tmp_path / "workspace"
    (root / "react-vite-app" / "src").mkdir(parents=True)
    (root / "react-vite-app" / "src" / "main.jsx").write_text(
        "console.log(1)\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# 说明\n", encoding="utf-8")
    return root


def _make_dir_link(target: Path, link: Path) -> bool:
    """尽量创建目录链接，环境不支持时返回 ``False``。

    WHY 两种方式都试：Windows 上建符号链接需要开发者模式或管理员权限，而目录
    联接（junction）普通用户即可创建；POSIX 上只有符号链接一种。
    """
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return True
        except Exception:
            pass
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


# ------------------------------------------------------------------ 正常解析


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("/", ""),
        ("", ""),
        ("/README.md", "README.md"),
        ("README.md", "README.md"),
        ("\\README.md", "README.md"),
        ("/react-vite-app/./src/main.jsx", "react-vite-app/src/main.jsx"),
        ("/react-vite-app//src//main.jsx", "react-vite-app/src/main.jsx"),
    ],
)
def test_resolves_virtual_paths(workspace: Path, given: str, expected: str):
    resolved = resolve_in_workspace(workspace, given)

    expected_path = (workspace / expected).resolve() if expected else workspace.resolve()
    assert resolved == expected_path


def test_resolves_path_that_does_not_exist_yet(workspace: Path):
    """不存在不是校验错误：列目录与读文件各自负责把「不存在」映射成 404，
    校验层提前报错会让 404 与 400 混在一起。"""
    assert resolve_in_workspace(workspace, "/not/created/yet.txt").name == "yet.txt"


# ------------------------------------------------------------------ 字符串级拦截


@pytest.mark.parametrize(
    ("given", "reason"),
    [
        ("/../outside.txt", "上级目录穿越"),
        ("../outside.txt", "纯相对穿越"),
        ("/a/../../outside.txt", "夹在中间的穿越"),
        ("/~/secret", "波浪号"),
        ("~/.ssh/id_rsa", "波浪号开头"),
        ("/C:/Windows/win.ini", "盘符"),
        ("C:/Windows/win.ini", "无前缀盘符"),
        ("/file.txt:stream", "数据流冒号"),
        ("/bad\x00name", "空字节"),
        (123, "非字符串"),
    ],
)
def test_rejects_illegal_paths(workspace: Path, given: object, reason: str):
    with pytest.raises(WorkspacePathError):
        resolve_in_workspace(workspace, given)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 解析级拦截


def test_rejects_symlink_escaping_workspace(workspace: Path, tmp_path: Path):
    """红线：字符串层面完全合法，但真实落点在工作区之外。

    ``/link/secret.txt`` 里没有 ``..``、不是绝对路径、也不带 ``~``；只有先把
    ``link`` 解析成真实路径再比较前缀才能发现它指向外面——这正是本模块存在的
    理由，也是「字符串级校验」不够用的地方。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret", encoding="utf-8")
    if not _make_dir_link(outside, workspace / "link"):
        pytest.skip("当前环境无法创建目录链接（Windows 需开发者模式或管理员权限）")

    with pytest.raises(WorkspacePathError, match="逃出工作区"):
        resolve_in_workspace(workspace, "/link/secret.txt")


def test_allows_symlink_staying_inside_workspace(workspace: Path):
    """指向工作区**内部**的链接必须放行：它不是逃逸，禁掉只会让正常工作区结构
    变得不可用。"""
    if not _make_dir_link(workspace / "react-vite-app", workspace / "app-link"):
        pytest.skip("当前环境无法创建目录链接")

    resolved = resolve_in_workspace(workspace, "/app-link/src/main.jsx")

    assert resolved == (workspace / "react-vite-app" / "src" / "main.jsx").resolve()
    # 解析结果是真实路径而非链接路径——这是「解析级」与「字符串级」的差别所在
    assert resolved.is_relative_to(workspace.resolve())


def test_to_virtual_path_round_trips(workspace: Path):
    target = workspace / "react-vite-app" / "src" / "main.jsx"

    assert to_virtual_path(workspace, target) == "/react-vite-app/src/main.jsx"
    assert to_virtual_path(workspace, workspace) == "/"


# ------------------------------------------------------------------ 列目录


def test_list_directory_puts_dirs_first(workspace: Path):
    listing = list_directory(workspace, "/", max_entries=100)

    assert listing.path == "/"
    assert listing.parent is None
    assert [item.name for item in listing.entries] == ["react-vite-app", "README.md"]
    assert listing.entries[0].is_dir is True
    assert listing.entries[0].path == "/react-vite-app"
    assert listing.entries[1].path == "/README.md"
    assert listing.truncated is False


def test_list_directory_reports_parent(workspace: Path):
    listing = list_directory(workspace, "/react-vite-app/src", max_entries=100)

    assert listing.path == "/react-vite-app/src"
    assert listing.parent == "/react-vite-app"
    assert [item.name for item in listing.entries] == ["main.jsx"]


def test_list_directory_marks_truncation(workspace: Path):
    for index in range(5):
        (workspace / f"file{index}.txt").write_text("x", encoding="utf-8")

    listing = list_directory(workspace, "/", max_entries=3)

    assert len(listing.entries) == 3
    # 截断必须显式标出：界面要能告诉用户「这里还有更多」，而不是假装目录就这么大
    assert listing.truncated is True


def test_list_directory_rejects_missing_and_file(workspace: Path):
    with pytest.raises(FileNotFoundError):
        list_directory(workspace, "/nope", max_entries=10)
    with pytest.raises(NotADirectoryError):
        list_directory(workspace, "/README.md", max_entries=10)


# ------------------------------------------------------------------ 读文件


def test_read_bytes_capped_reports_total_and_truncation(workspace: Path):
    target = workspace / "big.txt"
    target.write_text("x" * 100, encoding="utf-8")

    data, total, truncated = read_bytes_capped(target, max_bytes=10)

    assert len(data) == 10
    assert total == 100
    assert truncated is True


def test_looks_binary_by_content_not_suffix(workspace: Path):
    assert looks_binary(b"\x89PNG\r\n\x1a\n\x00\x00") is True
    # 没有空字节的文本即便后缀陌生也按文本处理：Makefile / LICENSE 都属此类
    assert looks_binary(b"all:\n\tpytest\n") is False
