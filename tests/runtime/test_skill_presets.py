"""场景预设（``preset.toml``）的加载与校验。

WHY 需要这一组用例：预设是**声明式配置**，而配置写错的表现通常是"静默少了一项能力"——
场景从下拉里消失、或白名单里的技能名打错后 Agent 就是不会那项技能。因此这里的断言集中在
「坏配置必须被报出来」，而不是"能读出来就行"。

另外还钉住一条交付物自检：仓库里真实交付的 ``skills/presets/*`` 必须都能加载——一个随包
发出去、运行时被静默跳过的场景，正是最糟的形态（代码里看得见、功能上不存在）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.skill_presets import (
    PRESET_FILE_NAME,
    PresetCatalog,
    PresetProblem,
    SkillPreset,
    load_presets,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
"""仓库根目录；真实交付的预设目录用来做交付物自检。"""


def _preset(root: Path, preset_id: str, body: str) -> Path:
    """在 ``root/<preset_id>/preset.toml`` 写一份预设文件。"""
    directory = root / preset_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / PRESET_FILE_NAME).write_text(body, encoding="utf-8")
    return directory


def _problem_of(catalog: PresetCatalog, preset_id: str) -> PresetProblem | None:
    for problem in catalog.problems:
        if Path(problem.directory).name == preset_id:
            return problem
    return None


# ------------------------------------------------------------------ 正常加载


def test_loads_a_complete_preset(tmp_path: Path) -> None:
    _preset(
        tmp_path,
        "coding",
        'title = "代码开发"\ndescription = "面向编程任务"\nskills = ["a", "b"]\n',
    )

    catalog = load_presets(tmp_path)

    assert catalog.ids == ["coding"]
    assert catalog.problems == ()
    preset = catalog.get("coding")
    assert preset is not None
    assert preset.title == "代码开发"
    assert preset.description == "面向编程任务"
    assert preset.skills == ("a", "b")
    assert preset.allows("a") is True
    assert preset.allows("c") is False


def test_title_defaults_to_the_preset_id(tmp_path: Path) -> None:
    """缺 title 时用目录名，而不是留下一个空白的场景名。"""
    _preset(tmp_path, "coding", 'skills = ["a"]\n')

    preset = load_presets(tmp_path).get("coding")

    assert preset is not None
    assert preset.title == "coding"


def test_an_empty_allowlist_means_unrestricted(tmp_path: Path) -> None:
    """省略 ``skills`` 表示"不限定"，而不是"什么都不允许"。

    WHY 单列：按「空 = 全禁」解释会让场景一创建就没有任何技能，且没有任何报错。
    """
    _preset(tmp_path, "coding", 'title = "代码开发"\n')

    preset = load_presets(tmp_path).get("coding")

    assert preset is not None
    assert preset.skills == ()
    assert preset.allows("anything") is True


def test_presets_are_sorted_by_id(tmp_path: Path) -> None:
    """清单按 ID 排序：界面的下拉顺序要稳定，否则每次刷新都在跳。"""
    for preset_id in ("writing", "coding"):
        _preset(tmp_path, preset_id, 'skills = ["a"]\n')

    assert load_presets(tmp_path).ids == ["coding", "writing"]


def test_a_missing_root_is_not_an_error(tmp_path: Path) -> None:
    """预设目录不存在时返回空结果——它是可选能力（与技能库同一口径）。"""
    catalog = load_presets(tmp_path / "nope")

    assert catalog.presets == ()
    assert catalog.problems == ()


def test_load_rejects_none() -> None:
    with pytest.raises(ValueError):
        load_presets(None)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 坏配置必须报出来


def test_missing_preset_file_is_reported(tmp_path: Path) -> None:
    (tmp_path / "coding").mkdir()

    catalog = load_presets(tmp_path)

    assert catalog.presets == ()
    assert _problem_of(catalog, "coding") is not None
    assert PRESET_FILE_NAME in str(_problem_of(catalog, "coding").reason)


@pytest.mark.parametrize(
    "preset_id",
    ["Coding", "coding_v2", "coding.v2", "coding-", "编码"],
    ids=["uppercase", "underscore", "dot", "trailing-hyphen", "non-ascii"],
)
def test_an_invalid_directory_name_is_reported(tmp_path: Path, preset_id: str) -> None:
    """目录名即场景 ID，必须能安全地进 URL / 日志 / 筛选参数。"""
    _preset(tmp_path, preset_id, 'skills = ["a"]\n')

    catalog = load_presets(tmp_path)

    assert catalog.presets == ()
    assert _problem_of(catalog, preset_id) is not None


def test_a_broken_toml_is_reported_not_raised(tmp_path: Path) -> None:
    """单个文件语法错误不能让整次巡检失败——好场景必须照常可用。"""
    _preset(tmp_path, "coding", "skills = [\n")  # 未闭合
    _preset(tmp_path, "writing", 'skills = ["a"]\n')

    catalog = load_presets(tmp_path)

    assert catalog.ids == ["writing"]
    assert _problem_of(catalog, "coding") is not None


def test_a_non_list_skills_field_is_reported(tmp_path: Path) -> None:
    _preset(tmp_path, "coding", 'skills = "a,b"\n')

    catalog = load_presets(tmp_path)

    assert catalog.presets == ()
    problem = _problem_of(catalog, "coding")
    assert problem is not None
    assert "数组" in problem.reason


def test_a_non_string_skill_entry_is_reported(tmp_path: Path) -> None:
    _preset(tmp_path, "coding", 'skills = ["a", 1]\n')

    catalog = load_presets(tmp_path)

    assert catalog.presets == ()
    assert _problem_of(catalog, "coding") is not None


def test_a_skill_name_with_surrounding_spaces_is_reported(tmp_path: Path) -> None:
    """``" a"`` 永远匹配不到技能名（技能名不允许空格），必须报而不是静默保留。"""
    _preset(tmp_path, "coding", 'skills = [" a"]\n')

    catalog = load_presets(tmp_path)

    assert catalog.presets == ()
    problem = _problem_of(catalog, "coding")
    assert problem is not None
    assert "空格" in problem.reason


def test_an_unknown_field_is_reported(tmp_path: Path) -> None:
    """未知字段一律报错：静默忽略会让"配了半天没生效"无从解释。"""
    _preset(tmp_path, "coding", 'title = "x"\nskillz = ["a"]\n')

    catalog = load_presets(tmp_path)

    assert catalog.presets == ()
    problem = _problem_of(catalog, "coding")
    assert problem is not None
    assert "未知字段" in problem.reason


def test_a_non_string_title_is_reported(tmp_path: Path) -> None:
    _preset(tmp_path, "coding", 'title = 3\nskills = ["a"]\n')

    catalog = load_presets(tmp_path)

    assert catalog.presets == ()
    assert _problem_of(catalog, "coding") is not None


# ------------------------------------------------------------------ 工具函数


def test_get_returns_none_for_unknown_or_empty_ids() -> None:
    """未知 ID 不抛异常：历史会话里可能记着已被删除的场景，由调用方决定怎么办。"""
    preset = SkillPreset(preset_id="coding", title="代码开发", description="")
    catalog = PresetCatalog(presets=(preset,))

    assert catalog.get("coding") is preset
    assert catalog.get(None) is None
    assert catalog.get("") is None
    assert catalog.get("nope") is None


# ------------------------------------------------------------------ 交付物自检


def test_shipped_presets_load_without_problems() -> None:
    """随仓库交付的场景预设必须都能加载，且没有我们这一层报出的问题。

    WHY 与 ``test_skills.py`` 的内置技能自检同一理由：随包发出去、运行时被静默跳过的场景，
    在代码里看得见、在功能上不存在——那正是最难被发现的一类缺陷。
    """
    catalog = load_presets(_REPO_ROOT / "skills" / "presets")

    assert catalog.problems == (), f"交付的场景有问题：{catalog.problems}"
    assert catalog.ids == ["coding", "writing"]
    # 每个场景至少声明了技能；白名单里的名字必须是合法技能名（小写、连字符）。
    for preset in catalog.presets:
        assert preset.title
        assert preset.skills, f"{preset.preset_id} 没有声明任何技能"
        for name in preset.skills:
            assert name == name.lower().strip(), name
