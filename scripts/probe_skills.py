"""技能库开工前探针：把「启停怎么做」这个设计问题钉在实测事实上。

背景：``agent/graph.py`` 把 ``skills=config.skill_source_paths()`` 透传给
``create_deep_agent``，后者原样交给 ``SkillsMiddleware(backend, sources=...)``。而
``SkillsMiddleware`` **没有逐技能过滤参数**——``sources`` 是一组目录，目录下符合条件的
技能都会被加载。于是「禁用某个技能」只有两条路：搬文件，或换一份来源列表。

因此第一个问题就是决定性的：**来源能不能直接指向技能目录本身**（``SKILL.md`` 位于该目录
根部）？若能，启停就只是「按库里的状态算出一份来源列表」，不必移动任何文件；若不能，
就只能把禁用的技能挪出扫描范围——那会让「目录只描述能力、库记录是否启用」这条设计作废。

其余三个问题决定了校验与报错口径：
2. 元数据缺失 / 非法（缺 ``description``、``name`` 与目录名不符）时是**整源失败**还是
   跳过单个技能？
3. 目录里没有 ``SKILL.md`` 时如何处理（用户很可能把辅助文件直接丢在来源目录下）。
4. 多个来源出现同名技能时谁生效（文档说后者覆盖前者，值得核实而不是照抄）。

用法：``python scripts/probe_skills.py``；退出码 ``0`` 全部有结论 / ``1`` 探针本身失败。
Windows 上语料含中文，故全程强制 UTF-8 输出。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from deepagents.backends import FilesystemBackend  # noqa: E402
from deepagents.middleware.skills import SkillsMiddleware  # noqa: E402

_VALID = """---
name: {name}
description: {description}
---

# {name} 技能正文

## 何时使用
- 当用户要求做与 {name} 相关的事时。
"""


def _write_skill(root: pathlib.Path, name: str, *, body: str | None = None, description: str = "示例技能") -> None:
    """在工作区的 ``skills/<name>/SKILL.md`` 写入一份技能包。"""
    directory = root / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        body if body is not None else _VALID.format(name=name, description=description),
        encoding="utf-8",
    )


def _load(backend: FilesystemBackend, sources: list[str]) -> tuple[list[str], list[str]]:
    """按给定来源加载技能，返回 ``(技能名列表, 加载告警)``。

    WHY 直接调 ``before_agent``：它是中间件真正读取技能的入口，且返回结构里就带着
    元数据与告警——比在探针里复刻一遍目录扫描更接近生产行为。
    """
    middleware = SkillsMiddleware(backend=backend, sources=sources)  # type: ignore[arg-type]
    update = middleware.before_agent({}, None, {})  # type: ignore[arg-type]
    if not update:
        return [], []
    return (
        [str(item["name"]) for item in update.get("skills_metadata", [])],
        list(update.get("skills_load_errors", [])),
    )


def probe_source_shape(root: pathlib.Path) -> list[str]:
    """问题 1：来源能否指向技能目录自身。"""
    print("=== 问题 1：来源的指向层级 ===")
    backend = FilesystemBackend(root_dir=root, virtual_mode=True)
    _write_skill(root, "code-review")
    _write_skill(root, "legacy-skill")

    parent_names, _ = _load(backend, ["/skills"])
    print(f"  sources=['/skills']            -> {parent_names}")

    single_names, single_errors = _load(backend, ["/skills/code-review"])
    print(f"  sources=['/skills/code-review'] -> {single_names} 告警={single_errors}")

    if single_names == ["code-review"]:
        print("  [OK  ] 来源可指向技能目录自身 → 启停只需按库状态算来源列表，无需搬文件")
    else:
        print("  [NO  ] 来源不能指向技能目录自身 → 只能把禁用的技能挪出扫描范围")

    # 顺带确认：逐个技能的来源列表能否替代父目录（这是启停方案的实际形态）
    per_skill, _ = _load(backend, ["/skills/code-review", "/skills/legacy-skill"])
    print(f"  sources=[逐技能两项]           -> {per_skill}")
    return parent_names


def probe_invalid_metadata(root: pathlib.Path) -> None:
    """问题 2：元数据非法时是整源失败还是跳过单个。"""
    print("\n=== 问题 2：元数据非法 ===")
    backend = FilesystemBackend(root_dir=root, virtual_mode=True)

    cases = {
        "miss-desc": "---\nname: miss-desc\n---\n\n# 缺 description\n",
        "bad-name": "---\nname: 与目录名不符\ndescription: 名字非法\n---\n\n# 正文\n",
        "no-front": "# 完全没有 frontmatter\n",
    }
    for directory, body in cases.items():
        (root / "skills" / directory).mkdir(parents=True, exist_ok=True)
        (root / "skills" / directory / "SKILL.md").write_text(body, encoding="utf-8")

    names, errors = _load(backend, ["/skills"])
    print(f"  加载到的技能：{names}")
    for error in errors:
        print(f"  告警：{error[:160]}")
    print("  [OK  ] 非法技能被跳过并留下告警，合法技能不受影响" if len(names) >= 3 else "  [NO  ] 非法技能影响了整源加载")


def probe_missing_skill_md(root: pathlib.Path) -> None:
    """问题 3：缺 SKILL.md 的子目录如何处理，以及扫描是否**递归**。

    WHY 递归这一问是决定性的：若扫描只到一层，把禁用的技能挪进 ``skills/.disabled/``
    就能实现启停（不搬出工作区、目录仍可见）；若会递归，挪进去的技能照样被加载，
    那条路直接不成立。
    """
    print("\n=== 问题 3：子目录缺 SKILL.md / 扫描深度 ===")
    backend = FilesystemBackend(root_dir=root, virtual_mode=True)
    stray = root / "skills" / "helper-files"
    stray.mkdir(parents=True, exist_ok=True)
    (stray / "helper.py").write_text("print('辅助脚本')\n", encoding="utf-8")

    # 深层目录里的技能：只到一层则不会被发现
    deep = root / "skills" / ".disabled" / "parked-skill"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "SKILL.md").write_text(
        _VALID.format(name="parked-skill", description="被停用的技能"), encoding="utf-8"
    )

    names, errors = _load(backend, ["/skills"])
    print(f"  加载到的技能：{names}")
    print(f"  告警条数：{len(errors)}（0 说明「缺 SKILL.md 的目录」被静默忽略）")
    if "parked-skill" in names:
        print("  [NO  ] 扫描会递归 → 「挪进子目录」不能实现启停")
    else:
        print("  [OK  ] 扫描不递归 → 把技能挪进 skills/.disabled/ 即可实现启停")


def _load_meta(backend: FilesystemBackend, sources: list[str]) -> list[dict[str, object]]:
    """加载技能并返回完整元数据（用于看清「谁生效」）。"""
    middleware = SkillsMiddleware(backend=backend, sources=sources)  # type: ignore[arg-type]
    update = middleware.before_agent({}, None, {})  # type: ignore[arg-type]
    return list(update.get("skills_metadata", [])) if update else []


def probe_override(root: pathlib.Path) -> None:
    """问题 4：同名技能跨来源时**具体哪一个**生效。"""
    print("\n=== 问题 4：同名技能跨来源的覆盖顺序 ===")
    backend = FilesystemBackend(root_dir=root, virtual_mode=True)
    for layer in ("layer-a", "layer-b"):
        target = root / layer / "shared"
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(
            _VALID.format(name="shared", description=f"来自 {layer}"), encoding="utf-8"
        )

    forward = _load_meta(backend, ["/layer-a", "/layer-b"])
    reversed_order = _load_meta(backend, ["/layer-b", "/layer-a"])
    # WHY 打印 description 而不只打印名字：只数个数的话，「后者覆盖前者」与「前者覆盖
    # 后者」都会得到「只有一个 shared」这个相同结果——那样这条探针等于什么都没验。
    forward_desc = [item.get("description") for item in forward]
    reversed_desc = [item.get("description") for item in reversed_order]
    print(f"  sources=[layer-a, layer-b] -> {forward_desc}")
    print(f"  sources=[layer-b, layer-a] -> {reversed_desc}")
    if forward_desc == ["来自 layer-b"] and reversed_desc == ["来自 layer-a"]:
        print("  [OK  ] 同名时**后面的来源**生效（与文档一致）")
    else:
        print("  [NO  ] 与文档「last one wins」不符，需要另找规则")


def main() -> int:
    """跑完四个问题。"""
    with tempfile.TemporaryDirectory(prefix="mah-skills-") as workdir:
        root = pathlib.Path(workdir)
        parent_names = probe_source_shape(root)
        probe_invalid_metadata(root)
        probe_missing_skill_md(root)
        probe_override(root)

    print(f"\n=== 结论：父目录来源共加载到 {len(parent_names)} 个技能 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
