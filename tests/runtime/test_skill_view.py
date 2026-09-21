"""技能物化视图：全量替换、失败不留半个视图，以及「视图缺失不静默降级」。

重点覆盖三类：

1. **全量替换**：视图最终必须**等同于**给定技能集。留下旧技能的后果是「我明明停用了它，
   Agent 还在用」——而且没有任何报错。
2. **失败不可留半个视图**：重建中途失败若留下半份内容，表现是「部分技能突然消失」。
   故先建全再替换，失败时旧视图保持完整。
3. **技能名是目录名**：它参与路径拼接，含分隔符或 `..` 的名字能写到视图之外。这是 T23
   安全红线在文件系统侧的入口，必须有断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime import skill_view
from config import VIRTUAL_SKILL_VIEW
from runtime.skill_view import (
    ViewEntry,
    discard_view,
    rebuild_view,
    sources_for_graph,
    validate_entry_name,
)

VIEW_SOURCE = "/.skills-active"
"""视图的虚拟路径；与 ``config.VIRTUAL_SKILL_VIEW`` 同值。"""


_SKILL = """---
name: {name}
description: {name} 的说明
---

# {name}
"""


def _skill(workspace: Path, name: str, *, folder: str = "skills", helper: bool = False) -> Path:
    """在 ``workspace/<folder>/<name>/`` 下建一个技能包，返回其绝对路径。"""
    directory = workspace / folder / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(_SKILL.format(name=name), encoding="utf-8")
    if helper:
        (directory / "helper.py").write_text("print('辅助')\n", encoding="utf-8")
    return directory


def _entry(workspace: Path, name: str) -> ViewEntry:
    """构造一个指向 ``workspace/skills/<name>`` 的视图条目。"""
    return ViewEntry(name=name, source_dir=workspace / "skills" / name)


def _view(workspace: Path) -> Path:
    """本次用例使用的视图目录。

    WHY 由用例自己给：视图已搬到**根外存储**（真实位置是
    ``<数据目录>/roots/<根标识>/skills-active``），布局由 ``config`` 决定，本模块只接收
    「那个目录」——因此用例传什么就是什么，不再有「工作区里的 .skills-active」这回事。
    """
    return workspace / "store" / "skills-active"


# --------------------------------------------------------------- 路径约定


def test_view_virtual_path_is_stable_and_dot_prefixed() -> None:
    """视图的**虚拟路径**固定且以点开头。

    WHY 固定：缓存的图持有的是来源**虚拟路径**，位置一变就会指向旧位置（表现为「改了
    启停却不生效」）。
    WHY 点开头：它是程序生成的产物；虽然已经不在工作区里（面板不会再遍历到），日志与
    虚拟路径仍要能一眼区分「技能包本体」与「它的派生物」。
    """
    assert VIRTUAL_SKILL_VIEW.startswith("/.")
    assert VIRTUAL_SKILL_VIEW.rstrip("/").endswith("skills-active")


def test_temp_directory_sits_next_to_the_view(tmp_path: Path) -> None:
    """构建中的临时目录与视图同处一层（跨层 ``rename`` 会失败）。

    WHY 单列：换层之后「替换视图」就不是原子操作了，失败时可能留下半个视图——而症状是
    部分技能静默消失。
    """
    view = _view(tmp_path)

    temp = skill_view.temp_directory(view)

    assert temp.parent == view.parent
    assert temp.name.startswith(view.name)


# --------------------------------------------------------------- 全量替换


def test_rebuild_copies_the_given_skills(tmp_path: Path) -> None:
    """列出的技能被复制进视图，含辅助脚本。"""
    _skill(tmp_path, "code-review", helper=True)
    _skill(tmp_path, "doc-to-markdown")

    result = rebuild_view(
        tmp_path, [_entry(tmp_path, "code-review"), _entry(tmp_path, "doc-to-markdown")]
    )

    assert result.copied == ("code-review", "doc-to-markdown")
    assert (result.view_path / "code-review" / "SKILL.md").is_file()
    # 辅助脚本必须一并复制：模型是按视图里的路径去读它的
    assert (result.view_path / "code-review" / "helper.py").is_file()


def test_rebuild_is_a_full_replacement(tmp_path: Path) -> None:
    """上一版有、这一版没有的技能必须从视图里消失。

    WHY：留下的旧技能会让「我明明停用了它，Agent 还在用」同时成立，且没有任何报错。
    """
    _skill(tmp_path, "code-review")
    _skill(tmp_path, "legacy")
    rebuild_view(_view(tmp_path), [_entry(tmp_path, "code-review"), _entry(tmp_path, "legacy")])

    result = rebuild_view(_view(tmp_path), [_entry(tmp_path, "code-review")])

    assert result.removed == ("legacy",)
    assert not (result.view_path / "legacy").exists()
    assert (result.view_path / "code-review").is_dir()


def test_rebuild_with_empty_set_yields_empty_view(tmp_path: Path) -> None:
    """全部停用时视图存在但为空——不是把视图删掉。

    WHY 保留空目录：「视图存在」是「启停状态已知」的标志，也是 fallback 判定的依据
    （见 ``sources_for_graph``）；删掉它会让全停用被误判成「视图丢了」。
    """
    _skill(tmp_path, "code-review")
    rebuild_view(_view(tmp_path), [_entry(tmp_path, "code-review")])

    result = rebuild_view(_view(tmp_path), [])

    assert result.view_path.is_dir()
    assert list(result.view_path.iterdir()) == []


def test_rebuild_skips_invalid_skill_but_keeps_others(tmp_path: Path) -> None:
    """源目录不是有效技能包时跳过它，其余照常。"""
    _skill(tmp_path, "code-review")
    broken = tmp_path / "skills" / "broken"
    broken.mkdir(parents=True, exist_ok=True)  # 没有 SKILL.md

    result = rebuild_view(
        tmp_path, [_entry(tmp_path, "code-review"), _entry(tmp_path, "broken")]
    )

    assert result.copied == ("code-review",)
    assert result.skipped == ("broken",)
    assert (result.view_path / "code-review").is_dir()
    assert not (result.view_path / "broken").exists()


def test_stale_building_directory_is_cleaned(tmp_path: Path) -> None:
    """上次崩溃留下的构建目录不会污染本次结果。"""
    _skill(tmp_path, "code-review")
    stale = skill_view.temp_directory(_view(tmp_path))
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "half-copied").mkdir()

    result = rebuild_view(_view(tmp_path), [_entry(tmp_path, "code-review")])

    assert result.copied == ("code-review",)
    assert not (result.view_path / "half-copied").exists()


# --------------------------------------------------------------- 失败不留半个视图


def test_failed_copy_keeps_the_previous_view_intact(tmp_path: Path, monkeypatch) -> None:
    """复制中途失败时，上一版视图必须仍然完整。

    WHY：失败若留下半份内容，表现是「部分技能突然消失」，而调用方拿到的是一个异常——
    两者对不上时排查方向会完全跑偏。
    """
    _skill(tmp_path, "code-review")
    _skill(tmp_path, "legacy")
    rebuild_view(_view(tmp_path), [_entry(tmp_path, "code-review"), _entry(tmp_path, "legacy")])

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("模拟复制失败")

    monkeypatch.setattr(skill_view, "_copy_skill", _boom)
    with pytest.raises(OSError):
        rebuild_view(_view(tmp_path), [_entry(tmp_path, "code-review")])

    # 旧视图完好：两个技能都还在
    assert (_view(tmp_path) / "code-review").is_dir()
    assert (_view(tmp_path) / "legacy").is_dir()


# --------------------------------------------------------------- 名字即目录名（安全）


def test_duplicate_names_are_rejected(tmp_path: Path) -> None:
    """重名直接报错，而不是让一个技能静默覆盖另一个。"""
    _skill(tmp_path, "code-review")

    with pytest.raises(ValueError, match="重复"):
        rebuild_view(_view(tmp_path), [_entry(tmp_path, "code-review"), _entry(tmp_path, "code-review")])


@pytest.mark.parametrize("bad", ["", "   ", ".", "..", "a/b", "a\\b"])
def test_unsafe_skill_names_are_rejected(bad: str) -> None:
    """含分隔符 / 上跳 / 空的名字不能当目录名。

    WHY 这是安全断言而不是格式洁癖：技能名会成为**视图里的目录名**并参与路径拼接，
    放行 ``..`` 或 ``a/b`` 就等于允许把内容写到视图之外。
    """
    with pytest.raises(ValueError):
        validate_entry_name(bad)


def test_traversal_name_cannot_escape_the_view(tmp_path: Path) -> None:
    """上跳名在重建阶段就被拦下，工作区之外不会出现任何东西。"""
    _skill(tmp_path, "code-review")
    outside = tmp_path / "outside" / "escaped"

    with pytest.raises(ValueError):
        rebuild_view(
            tmp_path, [ViewEntry(name="../outside/escaped", source_dir=tmp_path / "skills")]
        )

    assert not outside.exists()


def test_valid_entry_name_is_passed_through() -> None:
    """合规名字原样返回（去空白）。"""
    assert validate_entry_name("  code-review  ") == "code-review"


# --------------------------------------------------------------- 建图来源判定


def test_sources_use_the_view_when_it_exists(tmp_path: Path) -> None:
    """视图存在时，建图来源就是视图。"""
    _skill(tmp_path, "code-review")
    rebuild_view(_view(tmp_path), [_entry(tmp_path, "code-review")])

    sources, warning = sources_for_graph(_view(tmp_path), ["/skills"], VIEW_SOURCE)

    assert sources == [VIEW_SOURCE]
    assert warning == ""


def test_missing_view_falls_back_loudly(tmp_path: Path) -> None:
    """视图不存在时退回配置目录，**并给出告警原因**。

    WHY 这是 fail-loud 的核心：把「视图不存在」当成「没有技能」，会让全部技能静默消失，
    用户只看到 Agent 突然不会那些套路了。
    """
    sources, warning = sources_for_graph(_view(tmp_path), ["/skills"], VIEW_SOURCE)

    assert sources == ["/skills"]
    assert "不存在" in warning


def test_missing_view_without_configured_sources_is_quiet(tmp_path: Path) -> None:
    """既没有视图也没有配置来源时返回空，且不算异常。"""
    sources, warning = sources_for_graph(_view(tmp_path), [], VIEW_SOURCE)

    assert sources == []
    assert warning == ""


def test_blank_configured_sources_are_ignored(tmp_path: Path) -> None:
    """配置里的空白来源被忽略（尾部分隔符很常见）。"""
    sources, _ = sources_for_graph(_view(tmp_path), ["/skills", "  "], VIEW_SOURCE)

    assert sources == ["/skills"]


def test_discard_view_removes_view_and_build_directory(tmp_path: Path) -> None:
    """``discard_view`` 把视图与构建目录一起清掉。"""
    _skill(tmp_path, "code-review")
    rebuild_view(_view(tmp_path), [_entry(tmp_path, "code-review")])
    (skill_view.temp_directory(_view(tmp_path))).mkdir()

    discard_view(_view(tmp_path))

    assert not _view(tmp_path).exists()
