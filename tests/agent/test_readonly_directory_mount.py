"""只读目录挂载：读得到、写不动、出不去。

WHY 单独成文件（而不是并进 ``test_global_memory.py``）：那一组钉的是**单文件**挂载
（全局记忆，父目录必须不可见），这一组钉的是**整棵子树**挂载（技能库 / 技能视图 / 工具
留存）。两者的安全边界不同，混在一起会让「父目录可见性」这条断言看起来自相矛盾。

WHY 写拒绝的断言必须存在：``sandbox`` 档位下工具级文件权限规则已被停用（execute 走
shell，路径规则拦不住），挂载点自己就是那道边界——它一旦放行写，Agent 就能改技能库、
篡改物化视图，或往应用的数据目录里塞任意内容。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.readonly_mount import ReadOnlyDirectoryMount


def _mount(tmp_path: Path) -> tuple[ReadOnlyDirectoryMount, Path]:
    """造一个装着两个技能的目录挂载点，返回（挂载点, 宿主目录）。"""
    host = tmp_path / "store"
    (host / "code-review").mkdir(parents=True)
    (host / "code-review" / "SKILL.md").write_text("---\nname: code-review\n---\n", encoding="utf-8")
    (host / "notes.txt").write_text("言先生\n", encoding="utf-8")
    return ReadOnlyDirectoryMount(host, label="技能库"), host


# ------------------------------------------------------------------ 构造校验


@pytest.mark.parametrize(
    "host_dir, label",
    [(None, "技能库"), ("/tmp/x", "技能库"), (Path("/tmp/x"), "")],
    ids=["none", "not-a-path", "blank-label"],
)
def test_constructor_validates_its_inputs(host_dir: object, label: str) -> None:
    """构造参数必须校验：坏取值要在装配时炸，而不是等到某次读文件才炸。"""
    with pytest.raises(ValueError):
        ReadOnlyDirectoryMount(host_dir, label=label)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 读


def test_ls_and_read_delegate_to_the_host_directory(tmp_path: Path) -> None:
    """列目录与读文件都委托给宿主目录（Agent 看到的就是真实内容）。

    目录条目带尾斜杠（``/code-review/``）——那是上游的口径，照抄而不是改写：模型已经
    学会按这个形状区分目录与文件。
    """
    mount, _host = _mount(tmp_path)

    listing = mount.ls("/")
    assert listing.error is None
    assert {entry["path"] for entry in (listing.entries or [])} == {"/code-review/", "/notes.txt"}

    read = mount.read("/notes.txt")
    assert read.error is None
    assert read.file_data is not None
    assert "言先生" in read.file_data["content"]


def test_download_files_returns_content(tmp_path: Path) -> None:
    """按批取内容（技能中间件走的就是这个接口）。"""
    mount, _host = _mount(tmp_path)

    downloaded = mount.download_files(["/code-review/SKILL.md"])

    assert downloaded[0].error is None
    assert b"code-review" in (downloaded[0].content or b"")


def test_grep_and_glob_stay_inside_the_mount(tmp_path: Path) -> None:
    """检索与匹配只在这棵子树里。"""
    mount, _host = _mount(tmp_path)

    assert [item["path"] for item in (mount.grep("言先生").matches or [])] == ["/notes.txt"]
    assert [item["path"] for item in (mount.glob("**/*.md").matches or [])] == [
        "/code-review/SKILL.md"
    ]


# ------------------------------------------------------------------ 写：一律拒绝


@pytest.mark.parametrize(
    "action",
    [
        lambda mount: mount.write("/notes.txt", "改掉"),
        lambda mount: mount.edit("/notes.txt", "言先生", "张三"),
        lambda mount: mount.delete("/notes.txt"),
        lambda mount: mount.upload_files([("/notes.txt", b"hi")])[0],
    ],
    ids=["write", "edit", "delete", "upload"],
)
def test_every_write_is_refused(tmp_path: Path, action) -> None:
    """写、改、删、上传一律拒绝，且宿主内容不变。"""
    mount, host = _mount(tmp_path)

    result = action(mount)

    assert result.error is not None
    assert "只读" in result.error
    assert (host / "notes.txt").read_text(encoding="utf-8") == "言先生\n"


# ------------------------------------------------------------------ 越界


@pytest.mark.parametrize("path", ["/../secret.txt", "../../secret.txt"])
def test_paths_that_escape_the_mount_are_refused(tmp_path: Path, path: str) -> None:
    """越界路径必须给出**干净的失败结果**，而不是把上游的异常抛出去。

    WHY 单列这条细节：上游对 ``..`` 段是直接抛 ``ValueError: Path traversal not allowed``，
    而工具层抛异常会打断整轮图执行——「路径写错了」本可以是模型自己改一个参数就能继续的事。
    """
    mount, _host = _mount(tmp_path)

    result = mount.read(path)

    assert result.error is not None
    assert "不在本挂载点内" in result.error


def test_relative_paths_resolve_inside_the_mount(tmp_path: Path) -> None:
    """相对路径按上游口径解析（落在挂载点内，因此不构成越界）。

    WHY 保留这个行为而不是一律拒绝：``FilesystemBackend`` 把相对路径解析到自己的根下，
    与其余档位的后端一致；额外加一条「必须绝对路径」的规则会让同一份技能代码在不同档位
    下表现不同——而那种差异只会在换档位时才暴露。
    """
    mount, _host = _mount(tmp_path)

    read = mount.read("notes.txt")

    assert read.error is None
    assert read.file_data is not None


def test_write_refusal_names_the_host_directory(tmp_path: Path) -> None:
    """错误文案要带上宿主目录：否则读者不知道「只读的是哪一份」。"""
    mount, host = _mount(tmp_path)

    assert str(host) in str(mount.write("/notes.txt", "x").error)
    assert mount.describe().endswith("（技能库）")


def test_a_missing_directory_fails_reads_cleanly(tmp_path: Path) -> None:
    """宿主目录不存在时：读操作给出干净的失败，而不是抛异常打断整轮运行。"""
    mount = ReadOnlyDirectoryMount(tmp_path / "nope", label="技能库")

    assert mount.read("/x.txt").error is not None
    assert mount.grep("x").matches == []
