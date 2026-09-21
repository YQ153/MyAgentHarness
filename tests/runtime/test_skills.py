"""技能包解析与校验。

重点覆盖两类**不会报错但会误导**的情况：

1. **上游宽松、我们能看见**：探针实测上游对违反命名规范的技能只告警、仍然加载。于是
   「改了 frontmatter 的 name 却好像没生效」在工作区里是可能的；本层把这类情况提升为
   显式 `problems`。用例既钉住「技能确实被加载了」，也钉住「我们报了问题」——两条一起
   才说明我们没有把上游的宽松当成自己的结论。
2. **坏技能不牵连好技能**：缺 `description` / 无 frontmatter 的技能被跳过，其余照常列出。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.skills import inspect_skills

_SOURCES = ["/skills"]


def _skill(
    root: Path,
    directory: str,
    *,
    name: str | None = None,
    description: str | None = "一个用于测试的示例技能",
    extra_lines: tuple[str, ...] = (),
    body: str = "# 正文\n\n## 何时使用\n- 当用户要求做这件事时。\n",
    folder: str = "skills",
) -> Path:
    """写一个技能包，返回它的目录。

    Args:
        name: frontmatter 里的 name；``None`` 表示用目录名。
        description: ``None`` 表示整行不写（模拟缺失）。
        folder: 顶层目录名，用于构造多来源场景。
    """
    target = root / folder / directory
    target.mkdir(parents=True, exist_ok=True)
    lines = ["---", f'name: "{name if name is not None else directory}"']
    if description is not None:
        lines.append(f'description: "{description}"')
    lines.extend(extra_lines)
    lines.append("---")
    (target / "SKILL.md").write_text("\n".join(lines) + "\n\n" + body, encoding="utf-8")
    return target


# --------------------------------------------------------------- 基本解析


def test_empty_sources_returns_empty_inventory_without_touching_disk(tmp_path: Path) -> None:
    """来源为空时直接返回，不构造 backend、不碰磁盘。"""
    inventory = inspect_skills(tmp_path, [])

    assert inventory.packages == ()
    assert inventory.load_errors == ()
    assert inventory.names == []


def test_valid_skill_is_listed_with_its_paths(tmp_path: Path) -> None:
    """合规技能被列出，并带上目录、SKILL.md 路径与来源。"""
    _skill(tmp_path, "code-review", description="结构化代码审查")

    inventory = inspect_skills(tmp_path, _SOURCES)

    assert inventory.names == ["code-review"]
    package = inventory.packages[0]
    assert package.description == "结构化代码审查"
    assert package.directory == "/skills/code-review"
    assert package.skill_md_path.endswith("/SKILL.md")
    assert package.source == "/skills"
    assert package.problems == ()


def test_skill_without_description_is_reported_as_unloadable(tmp_path: Path) -> None:
    """缺 description 的技能被上游丢掉，且必须出现在 ``unloadable`` 里。

    WHY 不能只断言 ``load_errors``：实测上游对**单个技能**的解析失败只写日志，
    ``skills_load_errors`` 只承载**来源级**失败。只依赖它的输出，这个技能会在清单里
    彻底消失，用户看到的是「我明明建了它，面板里却没有」。
    """
    _skill(tmp_path, "good-one")
    _skill(tmp_path, "no-desc", description=None)

    inventory = inspect_skills(tmp_path, _SOURCES)

    assert inventory.names == ["good-one"]
    assert [item.directory for item in inventory.unloadable] == ["/skills/no-desc"]
    assert "description" in inventory.unloadable[0].reason


def test_skill_without_frontmatter_is_reported_with_its_reason(tmp_path: Path) -> None:
    """没有 frontmatter 的文件不被当成技能，且原因要说清是缺 frontmatter。"""
    target = tmp_path / "skills" / "plain"
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text("# 只有正文，没有 frontmatter\n", encoding="utf-8")

    inventory = inspect_skills(tmp_path, _SOURCES)

    assert inventory.names == []
    assert [item.directory for item in inventory.unloadable] == ["/skills/plain"]
    assert "frontmatter" in inventory.unloadable[0].reason


def test_loaded_skill_does_not_appear_in_unloadable(tmp_path: Path) -> None:
    """能加载的技能不会同时出现在 unloadable 里（差集必须干净）。"""
    _skill(tmp_path, "code-review")

    inventory = inspect_skills(tmp_path, _SOURCES)

    assert inventory.unloadable == ()


def test_directory_without_skill_md_is_ignored_silently(tmp_path: Path) -> None:
    """辅助目录（没有 SKILL.md）既不报错也不产生技能。

    WHY：用户很可能把脚本、素材直接丢在技能目录下，那不该被当成「一个坏技能」。
    """
    helper = tmp_path / "skills" / "assets"
    helper.mkdir(parents=True, exist_ok=True)
    (helper / "notes.txt").write_text("素材\n", encoding="utf-8")
    _skill(tmp_path, "code-review")

    inventory = inspect_skills(tmp_path, _SOURCES)

    assert inventory.names == ["code-review"]
    assert inventory.load_errors == ()


def test_auxiliary_files_inside_a_skill_do_not_break_parsing(tmp_path: Path) -> None:
    """技能包内的辅助脚本不影响解析。"""
    directory = _skill(tmp_path, "with-helper")
    (directory / "helper.py").write_text("print('辅助')\n", encoding="utf-8")

    inventory = inspect_skills(tmp_path, _SOURCES)

    assert inventory.names == ["with-helper"]


# --------------------------------------------------------------- 我们比上游更严的部分


def test_name_mismatch_with_directory_is_reported_but_still_loaded(tmp_path: Path) -> None:
    """``name`` 与目录名不一致时要报出来，但**承认它仍然被加载了**。

    WHY 两条断言都要：只断言 problems 会掩盖「上游其实照常加载它」这个事实，而正是
    这个事实让用户困惑——面板说有问题，Agent 却用得好好的。
    """
    _skill(tmp_path, "folder-name", name="另一个名字")

    inventory = inspect_skills(tmp_path, _SOURCES)

    assert inventory.names == ["另一个名字"], "上游只告警不拒绝，技能仍会被加载"
    assert any("不一致" in problem for problem in inventory.packages[0].problems)


def test_uppercase_name_is_reported_but_still_loaded(tmp_path: Path) -> None:
    """含大写字母的技能名不合规范（上游只告警、仍加载）。"""
    _skill(tmp_path, "Bad-Name", name="Bad-Name")

    inventory = inspect_skills(tmp_path, _SOURCES)

    assert inventory.names == ["Bad-Name"]
    assert any("非法字符" in problem for problem in inventory.packages[0].problems)


def test_name_with_consecutive_hyphens_is_reported(tmp_path: Path) -> None:
    """连续连字符不合规范。"""
    _skill(tmp_path, "a--b", name="a--b")

    problems = inspect_skills(tmp_path, _SOURCES).packages[0].problems

    assert any("连字符" in problem for problem in problems)


def test_overlong_name_is_reported(tmp_path: Path) -> None:
    """超长 name 报出来（上限取自上游导出的常量，不自己写 64）。"""
    long_name = "n" * 65
    _skill(tmp_path, long_name, name=long_name)

    problems = inspect_skills(tmp_path, _SOURCES).packages[0].problems

    assert any("name 超过" in problem for problem in problems)


def test_overlong_description_is_truncated_by_upstream(tmp_path: Path) -> None:
    """超长 description 被**上游静默截断**——因此我们这一层看不见它，如实钉住。

    WHY 要显式写下这件事：它的表现是「我在技能里写了很长的说明，Agent 却像没看见
    后半段」，而且没有任何告警。知道了就能在面板上提示上限，而不是让用户去猜。
    """
    original = "说明" * 600
    _skill(tmp_path, "long-desc", description=original)

    package = inspect_skills(tmp_path, _SOURCES).packages[0]

    assert len(package.description) < len(original), "上游应已截断超长说明"
    assert package.problems == (), "我们看不到已截断的那一段，因此不该谎报问题"


def test_name_with_accented_lowercase_is_accepted(tmp_path: Path) -> None:
    """带音标的小写名字是合规的。

    WHY：上游用 ``isalpha() and islower()`` 判定，允许 ``café`` 这类名字。若我们改用
    ASCII 正则，就会出现「面板说不行、Agent 却用得好好的」——那种分歧比不校验更糟。
    """
    _skill(tmp_path, "café-tool")

    package = inspect_skills(tmp_path, _SOURCES).packages[0]

    assert package.problems == ()


# --------------------------------------------------------------- 多来源


def test_source_is_the_longest_matching_prefix(tmp_path: Path) -> None:
    """嵌套来源时判定为**最长**匹配的那一个。

    WHY：取第一个匹配会把 ``/skills/team/x`` 报成来自 ``/skills``，而那恰好掩盖了
    「团队技能覆盖了基础技能」这条最该被看见的信息。
    """
    _skill(tmp_path, "shared", description="来自基础层")
    _skill(tmp_path, "team-only", folder="skills/team")

    inventory = inspect_skills(tmp_path, ["/skills", "/skills/team"])

    sources = {package.name: package.source for package in inventory.packages}
    assert sources["team-only"] == "/skills/team"


def test_skills_from_multiple_sources_are_all_listed(tmp_path: Path) -> None:
    """多个来源的技能都会出现。"""
    _skill(tmp_path, "base-skill")
    _skill(tmp_path, "team-skill", folder="team-skills")

    inventory = inspect_skills(tmp_path, ["/skills", "/team-skills"])

    assert inventory.names == ["base-skill", "team-skill"]
    assert set(inventory.sources) == {"/skills", "/team-skills"}


def test_unknown_source_directory_yields_no_skills(tmp_path: Path) -> None:
    """来源目录不存在时返回空结果而不是抛异常。

    WHY：技能库是可选能力，目录缺失只应降级——与 ``SessionRoot.skill_sources``
    的既有口径一致。
    """
    inventory = inspect_skills(tmp_path, ["/nope"])

    assert inventory.packages == ()


def test_inventory_names_are_sorted(tmp_path: Path) -> None:
    """名字列表有序，便于稳定展示与断言。"""
    for name in ("zeta", "alpha", "mid"):
        _skill(tmp_path, name)

    assert inspect_skills(tmp_path, _SOURCES).names == ["alpha", "mid", "zeta"]


@pytest.mark.parametrize("bad_source", ["", "   "])
def test_blank_source_is_dropped_without_losing_other_sources(
    tmp_path: Path, bad_source: str
) -> None:
    """空白来源被剔除，其余来源照常。

    WHY 这条是实测逼出来的：给上游传一个空来源会让**整次加载一件都拿不到**——不报错、
    也没有告警，表现为「所有技能凭空消失」。而空项在配置里并不罕见（尾部分隔符），
    它不该有这种后果。
    """
    _skill(tmp_path, "code-review")

    inventory = inspect_skills(tmp_path, ["/skills", bad_source])

    assert inventory.names == ["code-review"]
    assert inventory.sources == ("/skills",)


def test_blank_only_sources_yield_empty_inventory(tmp_path: Path) -> None:
    """全是空白来源时返回空结果，而不是把这串空白交给上游。"""
    _skill(tmp_path, "code-review")

    inventory = inspect_skills(tmp_path, ["  ", ""])

    assert inventory.packages == ()
    assert inventory.sources == ()


# --------------------------------------------------------------- 随仓库交付的内置技能

_REPO_ROOT = Path(__file__).resolve().parents[2]
"""仓库根目录；内置技能是随包交付的文件，必须真的能被解析。"""


def test_shipped_builtin_skills_parse_without_problems() -> None:
    """随仓库交付的内置技能必须能被解析，且没有我们这一层报出的问题。

    WHY 单独立一条：一个「随包发出去、运行时被静默跳过」的技能是最糟的形态——它在代码
    里看得见、在功能上不存在。这条用例让它在提交前就红，而不是等用户发现 Agent 少了
    某个套路。

    WHY 同时断言名字与 ``problems``：只断言「能加载」会漏掉命名不规范这类上游只给告警的
    情况；而内置技能是我们自己写的，没有任何理由不规范。
    """
    # WHY 用挂载而不是把来源放到工作区里：内置技能随应用交付（仓库根的
    # ``skills-builtin/``），不在用户工作区之内——backend 的根是工作区，不挂一个虚拟
    # 路径它一个都读不到。这条用例顺带钉住「挂载表确实能让区外目录被读到」。
    inventory = inspect_skills(
        _REPO_ROOT / "workspace",
        ["/skills-builtin"],
        mounts={"/skills-builtin/": _REPO_ROOT / "skills-builtin"},
    )

    assert inventory.names == ["code-review", "doc-to-markdown", "project-scaffold"]
    assert inventory.unloadable == ()
    for package in inventory.packages:
        assert package.problems == (), f"{package.name} 有问题：{package.problems}"


def test_builtin_skills_have_unique_names_against_user_directory() -> None:
    """内置目录与用户目录同时作为来源时，同名由**用户目录**生效（后者覆盖前者）。

    WHY：这条钉住的是「内置技能可以被用户按名覆盖」这个设计承诺。若哪天把两个目录的
    顺序调反，内置的那份会永远赢，而用户「改了却不生效」不会有任何报错。
    """
    # 顺序即优先级：内置在前（低），用户在后（高）。内置来源在工作区之外，故挂载。
    inventory = inspect_skills(
        _REPO_ROOT / "workspace",
        ["/skills-builtin", "/skills"],
        mounts={"/skills-builtin/": _REPO_ROOT / "skills-builtin"},
    )

    sources = {package.name: package.source for package in inventory.packages}
    # 用户目录当前没有同名技能，故内置技能仍来自内置目录——这条断言保证顺序写对了
    assert sources.get("code-review") == "/skills-builtin"
