"""技能库服务：清单、启停与视图重建的编排。

重点覆盖三处：

1. **顺序**：状态必须先落库、再重建视图。反过来的话，写库失败会留下「视图里有、库里
   没有」的技能，而那种不一致没人能解释。
2. **启停真的改变了视图**：只把状态写进库、没重建视图，表现是「我停用了它，Agent 还在
   用」——而且不会报错。所以每条启停用例都断言视图内容。
3. **诊断不许丢**：上游对单个技能的解析失败只写日志，必须在清单里被报出来，否则用户
   只看到「我明明建了它，面板里却没有」。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from application.errors import NotFoundError
from application.skill_service import (
    CATEGORY_GENERAL,
    CATEGORY_PRESET,
    CATEGORY_USER,
    SkillService,
)
from config import AppConfig, SessionRoot
from runtime.skill_store import open_skill_store
from tests.conftest import make_config, make_root

_SKILL = """---
name: {name}
description: {name} 的说明
---

# {name}
"""


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[tuple[SkillService, AppConfig]]:
    """临时工作区里的技能服务（技能目录指向工作区内的 ``skills/``）。"""
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    config = make_config(tmp_path, workspace=workspace, skill_dirs=[workspace / "skills"])
    async with open_skill_store(tmp_path / "skills-state.db") as store:
        yield SkillService(config, scope=make_root(config), store=store), config


def _write_skill(config: AppConfig, name: str, *, body: str | None = None) -> None:
    """在用户技能目录里写一个技能包。"""
    directory = Path(make_root(config).root) / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        body if body is not None else _SKILL.format(name=name), encoding="utf-8"
    )


def _view_names(config: AppConfig) -> set[str]:
    """技能视图里当前有哪些技能（视图在工作区的 ``.harness/`` 下，见 ``SessionRoot.skill_view_store``）。"""
    view = make_root(config).skill_view_store
    return {child.name for child in view.iterdir() if child.is_dir()} if view.is_dir() else set()


# --------------------------------------------------------------- 清单


async def test_list_reports_skills_enabled_by_default(
    service: tuple[SkillService, AppConfig],
) -> None:
    """没有任何记录时技能是启用的（技能包放进来的下一刻就该能用）。"""
    skills, config = service
    _write_skill(config, "code-review")

    listed = await skills.list_skills()

    assert [item["name"] for item in listed["items"]] == ["code-review"]
    assert listed["items"][0]["enabled"] is True
    assert listed["items"][0]["source"] == "/skills"


async def test_list_implements_default_scope(service: tuple[SkillService, AppConfig]) -> None:
    """默认作用域是全局。"""
    skills, _ = service

    assert (await skills.list_skills())["scope"] == "global"


async def test_list_reports_unloadable_candidates_with_reason(
    service: tuple[SkillService, AppConfig],
) -> None:
    """没能加载的候选必须出现在清单里，并带上原因。

    WHY：上游对单个技能的解析失败只写日志、不放进它的返回值。不在这里报出来，用户看到
    的就是「我明明建了它，面板里却没有」，且没有任何可查的线索。
    """
    skills, config = service
    broken = Path(make_root(config).root) / "skills" / "no-desc"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "SKILL.md").write_text('---\nname: "no-desc"\n---\n\n# 正文\n', encoding="utf-8")

    listed = await skills.list_skills()

    assert listed["items"] == []
    assert [item["directory"] for item in listed["unloadable"]] == ["/skills/no-desc"]
    assert "description" in listed["unloadable"][0]["reason"]


async def test_list_reports_problems_that_upstream_only_warns_about(
    service: tuple[SkillService, AppConfig],
) -> None:
    """上游只告警的问题（如 name 与目录名不符）要在清单里显式报出来。"""
    skills, config = service
    _write_skill(
        config,
        "folder-name",
        body='---\nname: "另一个名字"\ndescription: "说明"\n---\n\n# 正文\n',
    )

    listed = await skills.list_skills()

    assert listed["items"][0]["name"] == "另一个名字"
    assert any("不一致" in problem for problem in listed["items"][0]["problems"])


# --------------------------------------------------------------- 视图


async def test_refresh_view_creates_view_with_enabled_skills(
    service: tuple[SkillService, AppConfig],
) -> None:
    """重建视图后，视图里是全部启用中的技能。"""
    skills, config = service
    _write_skill(config, "code-review")
    _write_skill(config, "legacy")

    result = await skills.refresh_view()

    assert set(result.copied) == {"code-review", "legacy"}
    assert _view_names(config) == {"code-review", "legacy"}


async def test_view_copies_helper_files(service: tuple[SkillService, AppConfig]) -> None:
    """视图复制的是整个技能包（辅助脚本必须一并过去）。

    WHY：模型是按**视图里的路径**去读辅助文件的，只复制 SKILL.md 会让那些引用在运行时
    指向不存在的路径。
    """
    skills, config = service
    _write_skill(config, "with-helper")
    helper = Path(make_root(config).root) / "skills" / "with-helper" / "helper.py"
    helper.write_text("print('辅助')\n", encoding="utf-8")

    await skills.refresh_view()

    assert (make_root(config).skill_view_store / "with-helper" / "helper.py").is_file()


async def test_graph_sources_use_the_view_once_it_exists(
    service: tuple[SkillService, AppConfig],
) -> None:
    """视图建好之后，建图来源就是视图。"""
    skills, config = service
    _write_skill(config, "code-review")

    before, before_warning = skills.graph_sources()
    await skills.refresh_view()
    after, after_warning = skills.graph_sources()

    assert before == ["/skills"] and "不存在" in before_warning
    assert after == ["/.skills-active"] and after_warning == ""


# --------------------------------------------------------------- 启停


async def test_disable_removes_skill_from_view(service: tuple[SkillService, AppConfig]) -> None:
    """停用后该技能必须从视图里消失。

    WHY 断言视图而不是断言库：只写库不重建视图的表现是「我停用了它，Agent 还在用」，
    而两者都不会报错——所以只有视图内容能证明启停真的生效了。
    """
    skills, config = service
    _write_skill(config, "code-review")
    _write_skill(config, "legacy")
    await skills.refresh_view()

    record = await skills.set_enabled("legacy", False)

    assert record["enabled"] is False
    assert record["view"]["enabled_skills"] == ["code-review"]
    # WHY 断言字段名而不只是断言值：store 的记录用列名 ``skill_name``，而清单与接口里是
    # ``name``。若这里直接把记录展开（``{**record}``），列名就泄进了 API——同一个东西在
    # 「清单」与「启停」两处叫不同名字。值全对、名字不同，这种偏差只有断字段名才拦得住。
    assert record["name"] == "legacy"
    assert "skill_name" not in record
    assert _view_names(config) == {"code-review"}


async def test_enable_puts_skill_back(service: tuple[SkillService, AppConfig]) -> None:
    """重新启用后它回到视图里。"""
    skills, config = service
    _write_skill(config, "legacy")
    await skills.set_enabled("legacy", False)

    await skills.set_enabled("legacy", True)

    assert _view_names(config) == {"legacy"}


async def test_disabled_skill_still_listed_with_state(
    service: tuple[SkillService, AppConfig],
) -> None:
    """停用的技能仍出现在清单里（否则用户无法把它启用回来）。"""
    skills, config = service
    _write_skill(config, "legacy")
    await skills.set_enabled("legacy", False)

    listed = await skills.list_skills()

    assert [item["name"] for item in listed["items"]] == ["legacy"]
    assert listed["items"][0]["enabled"] is False


async def test_disabling_all_yields_empty_view(
    service: tuple[SkillService, AppConfig],
) -> None:
    """全部停用时视图存在但为空——不是把视图删掉（删掉会被判成「视图丢了」）。"""
    skills, config = service
    _write_skill(config, "legacy")

    await skills.set_enabled("legacy", False)

    assert make_root(config).skill_view_store.is_dir()
    assert _view_names(config) == set()


async def test_unknown_skill_name_is_rejected(service: tuple[SkillService, AppConfig]) -> None:
    """启停一个不存在的技能名要报错，而不是留下一条永不生效的记录。

    WHY：技能名多来自界面或模型生成，写错一个字母会留下一条看起来完全正常、却永不生效
    的 ghost 记录——用户以为自己停用了它，实际什么都没发生。
    """
    skills, _ = service

    with pytest.raises(NotFoundError):
        await skills.set_enabled("不存在的技能", False)


async def test_state_survives_new_service_instance(
    tmp_path: Path, service: tuple[SkillService, AppConfig]
) -> None:
    """启停状态持久化：换一个服务实例仍然有效（库是真相）。"""
    skills, config = service
    _write_skill(config, "legacy")
    await skills.set_enabled("legacy", False)

    async with open_skill_store(tmp_path / "skills-state.db") as store:
        reopened = SkillService(config, scope=make_root(config), store=store)
        listed = await reopened.list_skills()

    assert listed["items"][0]["enabled"] is False


async def test_builtin_skills_land_in_a_fresh_workspace_view(tmp_path: Path) -> None:
    """默认技能目录下，随应用交付的内置技能必须真的进入新工作区的视图。

    WHY 端到端跑一遍：内置技能在工作区**之外**，它的加载链有四段——挂虚拟路径 →
    巡检读到 → 反解回宿主机目录 → 复制进视图。任一段断了都只留一行 WARNING，
    而 Agent 会静默地少掉那几个套路（它照样能回答，只是不再会那些本事）。
    """
    workspace = tmp_path / "project"
    workspace.mkdir()
    # 显式传空列表 = 用默认派生规则：通用目录 + 预设目录 + 工作区内的技能库。
    # WHY 把预设目录指到一个不存在的目录：本用例只关心「通用技能能否走完加载链」，而随仓库
    # 交付的预设（coding / writing）会带进它们自己的技能，让预期集合依赖于交付物内容。
    config = make_config(
        tmp_path, workspace=workspace, skill_dirs=[], presets_dir=tmp_path / "no-presets"
    )

    async with open_skill_store(tmp_path / "skills-state.db") as store:
        skills = SkillService(config, scope=make_root(config), store=store)
        result = await skills.refresh_view()
        listed = await skills.list_skills()

    expected = {"code-review", "doc-to-markdown", "project-scaffold"}
    assert set(result.copied) == expected
    assert set(result.skipped) == set()
    assert _view_names(config) == expected
    # 来源注明在视图之外的内置目录，用户据此能解释「这个技能是随产品来的」
    assert {item["source"] for item in listed["items"]} == {"/skills-builtin"}
    assert all(item["enabled"] for item in listed["items"])


# --------------------------------------------------------------- 场景（预设）过滤


def _preset_config(
    tmp_path: Path, *, preset_id: str = "coding", skills: tuple[str, ...] = ("keep",)
) -> AppConfig:
    """构造带**隔离预设目录**的配置（不碰随应用交付的那一份）。"""
    presets = tmp_path / "presets"
    directory = presets / preset_id
    directory.mkdir(parents=True, exist_ok=True)
    listed = ", ".join(f'"{name}"' for name in skills)
    (directory / "preset.toml").write_text(
        f'title = "场景 {preset_id}"\nskills = [{listed}]\n', encoding="utf-8"
    )
    return make_config(tmp_path, presets_dir=presets)


def _scoped(config: AppConfig, preset_id: str) -> SessionRoot:
    """把测试用的根绑定到某个场景上。"""
    return SessionRoot(config, make_root(config).root, preset_id)


async def test_preset_allowlist_filters_the_view(tmp_path: Path) -> None:
    """视图只收场景白名单内的技能——白名单外的即使启用也不进。

    WHY 这是场景体系的验收点：场景的全部含义就是「这项任务用这几项技能」。白名单不起作用
    时，场景选择在界面上看得见、在行为上不存在。
    """
    config = _preset_config(tmp_path, skills=("keep",))
    async with open_skill_store(tmp_path / "s.db") as store:
        skills = SkillService(config, scope=_scoped(config, "coding"), store=store)
        _write_skill(config, "keep")
        _write_skill(config, "outside")
        result = await skills.refresh_view()
        listed = await skills.list_skills()

    assert set(result.copied) == {"keep"}
    assert set(_view_names(config)) == {"keep"}
    by_name = {item["name"]: item for item in listed["items"]}
    assert by_name["keep"]["in_preset"] is True
    assert by_name["outside"]["in_preset"] is False


async def test_without_a_preset_every_enabled_skill_is_visible(tmp_path: Path) -> None:
    """未绑定场景时不做白名单过滤——与改造前的行为一致（向后兼容）。"""
    config = _preset_config(tmp_path, skills=("keep",))
    async with open_skill_store(tmp_path / "s.db") as store:
        skills = SkillService(config, scope=make_root(config), store=store)
        _write_skill(config, "keep")
        _write_skill(config, "outside")
        await skills.refresh_view()

    # 断言包含关系而不是相等：随应用交付的通用技能**始终**在视图里（它们是每个场景的底座），
    # 而交付物内容会随版本变化——把内置技能名抄进断言会让这条用例在无关改动上红。
    assert {"keep", "outside"} <= set(_view_names(config))
    # 而预设目录里的技能**不在**视图里：未绑定场景时它连来源都不是。
    assert "from-preset" not in _view_names(config)


async def test_an_unknown_preset_falls_back_to_unrestricted(tmp_path: Path) -> None:
    """场景不存在时按「不限定」处理，而不是报错或清空视图。

    WHY：场景 ID 是历史会话里记着的东西，而场景目录可能被删或改名——此时让这条会话打不开，
    比「按不限定继续用」糟糕得多。
    """
    config = _preset_config(tmp_path, skills=("keep",))
    async with open_skill_store(tmp_path / "s.db") as store:
        skills = SkillService(config, scope=_scoped(config, "removed"), store=store)
        _write_skill(config, "outside")
        await skills.refresh_view()
        listed = await skills.list_skills()

    # 同上：通用技能始终在，这里只断言"没有因为场景不存在而少掉用户技能"。
    assert "outside" in _view_names(config)
    assert listed["preset"] is None
    assert listed["preset_id"] == "removed"


async def test_preset_and_toggle_both_apply(tmp_path: Path) -> None:
    """场景白名单与用户启停是两个独立维度：两者都满足才进视图。"""
    config = _preset_config(tmp_path, skills=("a", "b"))
    async with open_skill_store(tmp_path / "s.db") as store:
        skills = SkillService(config, scope=_scoped(config, "coding"), store=store)
        _write_skill(config, "a")
        _write_skill(config, "b")
        await skills.set_enabled("b", False)

    assert set(_view_names(config)) == {"a"}


async def test_missing_skills_in_the_allowlist_are_reported(tmp_path: Path) -> None:
    """白名单里写了、但来源中找不到的技能名要报出来。

    WHY 单列：名字打错或技能包没交付时，视图会安静地少一项——那正是「我选了场景，Agent 却
    不会那项技能」这种无从解释的现象。
    """
    config = _preset_config(tmp_path, skills=("keep", "ghost"))
    async with open_skill_store(tmp_path / "s.db") as store:
        skills = SkillService(config, scope=_scoped(config, "coding"), store=store)
        _write_skill(config, "keep")
        listed = await skills.list_skills()

    assert listed["missing_skills"] == ["ghost"]
    assert listed["preset"]["id"] == "coding"


async def test_skill_category_reflects_its_source(tmp_path: Path) -> None:
    """分类由来源推导：通用 / 预设 / 用户三类各归各位。"""
    config = _preset_config(tmp_path, skills=("from-preset",))
    package = config.skill_presets_dir / "coding" / "from-preset"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(_SKILL.format(name="from-preset"), encoding="utf-8")

    async with open_skill_store(tmp_path / "s.db") as store:
        # 绑定到该场景：预设目录只有在**当前场景**下才作为来源（否则会看到别的场景的技能）。
        skills = SkillService(config, scope=_scoped(config, "coding"), store=store)
        _write_skill(config, "from-user")
        listed = await skills.list_skills()

    categories = {item["name"]: item["category"] for item in listed["items"]}
    assert categories["code-review"] == CATEGORY_GENERAL
    assert categories["from-preset"] == CATEGORY_PRESET
    assert categories["from-user"] == CATEGORY_USER
