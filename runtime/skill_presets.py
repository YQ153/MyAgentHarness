"""场景预设（preset）：把「某种场景该用哪些技能」表达成一份可复现的配置。

WHY 用 TOML 文件而不是数据库表：

1. **技能本身就是文件资产**。预设的「技能清单」与技能包住在同一个目录树里
   （``skills/presets/<id>/``），配置与内容同源；放进数据库会立刻产生两个真相——
   库里写着用 ``code-review``，而磁盘上那个技能包被删了，谁说了算？
2. **无 schema 迁移**。加字段、改默认值不需要迁移脚本，也不必兼容旧库。
3. **可交付、可评审**。预设随版本交付，与代码一起被 diff 与 review；数据库里的行做不到。
4. **坏了能报出来**。解析失败是「这个场景不可用」，可以像技能诊断（``unloadable``）那样
   如实报出；而数据库语义下的失败常表现为「场景静默变空」。

代价与边界：预设**不是运行期可编辑**的（要改就改文件）。若将来确实需要在界面上增删场景，
那应当另加一层「用户覆盖」，而不是把这份配置搬进库——两层真相的教训见上面第 1 条。

本模块只做「读盘 + 校验 + 归属判断」，不关心技能包在哪、怎么解析——那是
``runtime.skills`` 的职责；它也不做过滤，过滤发生在 ``SkillService`` 重建视图时。
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

PRESET_FILE_NAME = "preset.toml"
"""场景描述文件名：每个场景目录下一份（与技能包的 ``SKILL.md`` 同一层级）。"""

_KNOWN_KEYS = frozenset({"title", "description", "skills"})
"""允许出现的键。未知键一律报为问题——静默忽略会让「配了半天没生效」无从解释。"""

PRESET_ID_RE = re.compile(r"^[a-z0-9](?:-?[a-z0-9])*$")
"""场景 ID（取自目录名）的字符集：小写字母、数字与单个连字符，且不以连字符开头或结尾。

与技能名同一口径（见 ``runtime.skills._name_problem``：同样禁止 ``--`` 与首尾连字符），
因为场景 ID 会进 URL、日志与筛选参数——允许大写、下划线或首尾连字符，会让「同一个场景」
在不同入口写成不同字符串（而它们指向同一个目录，排障时极难看出）。

WHY 公开：会话元数据表（``runtime.thread_store``）在**写入前**也要用同一套规则校验场景
ID。两处各写一份正则，迟早出现「库里存得进、场景目录却匹配不上」这种只有靠猜才能解释的
不一致。
"""

MAX_PRESET_ID_CHARS = 64
"""场景 ID 的长度上限。与技能名上限一致：它是目录名，也是 URL 里的一段。"""

_MAX_SKILLS = 64
"""一个场景最多声明的技能数。超出说明配置错位（例如把技能名列表写成了路径清单）。"""


@dataclass(frozen=True)
class SkillPreset:
    """一个场景预设。

    Attributes:
        preset_id: 场景标识，取自所在目录名（与 ``preset.toml`` 是否声明 id 无关——目录名
            是唯一权威，避免"文件里写 A、目录叫 B"这类两份说法）。
        title: 界面上显示的场景名。
        description: 一句话说明这个场景适合什么任务。
        skills: **技能名白名单**。空元组表示「不限定」——该场景接受全部可用技能。
    """

    preset_id: str
    title: str
    description: str
    skills: tuple[str, ...] = ()

    def allows(self, skill_name: str) -> bool:
        """该技能是否属于本场景。

        WHY 空白名单表示「不限定」而不是「什么都不允许」：``skills`` 省略时最可能的意图是
        「这个场景不做额外限制」，而按「空 = 全禁」解释会让场景一创建就没有任何技能，
        且没有任何报错。
        """
        if not self.skills:
            return True
        return skill_name in self.skills


@dataclass(frozen=True)
class PresetProblem:
    """一个**没能加载**的候选场景。

    Attributes:
        directory: 候选目录的绝对路径（字符串形式，便于直接进 API 响应）。
        reason: 为什么它不可用（人类可读，面向配置作者而不是最终用户）。
    """

    directory: str
    reason: str


@dataclass(frozen=True)
class PresetCatalog:
    """一次预设目录巡检的结果。

    Attributes:
        presets: 加载成功的场景（按 ID 排序，便于稳定展示与断言）。
        problems: 没能加载的候选目录及其原因。
    """

    presets: tuple[SkillPreset, ...] = ()
    problems: tuple[PresetProblem, ...] = ()

    @property
    def ids(self) -> list[str]:
        """全部场景 ID（已排序）。"""
        return [preset.preset_id for preset in self.presets]

    def get(self, preset_id: str | None) -> SkillPreset | None:
        """按 ID 取场景；``None`` / 空串 / 未知 ID 都返回 ``None``。

        WHY 未知 ID 不抛异常：它在**解析期**只是一个"没有这个场景"的事实，由调用方决定
        是报错还是按「不限定」处理（历史会话里可能记着已被删除的场景 ID，那时让它打不开
        历史才是更糟的选择）。
        """
        if not preset_id:
            return None
        for preset in self.presets:
            if preset.preset_id == preset_id:
                return preset
        return None


def _problem(directory: Path, reason: str) -> PresetProblem:
    """构造一个问题项并记一行 WARNING。

    WHY 统一在这里打日志：巡检由面板、装配与测试多条路径触发，散在各处打日志必然有人漏
    打——而「场景没加载」正是那种"不报错、只是没有效果"的故障。
    """
    logger.warning("场景预设不可用：%s（%s）", directory, reason)
    return PresetProblem(directory=str(directory), reason=reason)


def _parse_skills(raw: object, directory: Path) -> tuple[tuple[str, ...], PresetProblem | None]:
    """解析 ``skills`` 字段，返回 ``(白名单, 问题或 None)``。

    WHY 有一个非法元素就整体判为问题、而不是跳过那一个：白名单是"这个场景应该有哪些技能"
    的声明，少了一个就意味着场景能力不完整；静默跳过会让「我明明写了它」变成一个查不出
    原因的现象。
    """
    if raw is None:
        return (), None
    if not isinstance(raw, list):
        return (), _problem(directory, f"skills 必须是字符串数组，实际：{type(raw).__name__}")
    if len(raw) > _MAX_SKILLS:
        return (), _problem(directory, f"skills 超过上限 {_MAX_SKILLS}（当前 {len(raw)}）")

    skills: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            return (), _problem(directory, f"skills[{index}] 必须是非空字符串")
        name = item.strip()
        if name != item:
            # 前后空格不会被技能名匹配到（技能名不允许空格），静默通过等于写了个永不生效的名字。
            return (), _problem(directory, f"skills[{index}] 含首尾空格：{item!r}")
        skills.append(name)
    return tuple(skills), None


def _load_one(directory: Path) -> SkillPreset | PresetProblem:
    """读取单个场景目录；失败时返回问题项而不是抛异常。"""
    preset_id = directory.name
    if len(preset_id) > MAX_PRESET_ID_CHARS:
        return _problem(directory, f"目录名超过 {MAX_PRESET_ID_CHARS} 字符：{preset_id!r}")
    if not PRESET_ID_RE.match(preset_id):
        return _problem(
            directory,
            f"目录名不能作为场景 ID（只允许小写字母、数字与单个连字符，"
            f"且不以连字符开头或结尾）：{preset_id!r}",
        )

    preset_file = directory / PRESET_FILE_NAME
    if not preset_file.is_file():
        return _problem(directory, f"缺少 {PRESET_FILE_NAME}")

    try:
        raw = tomllib.loads(preset_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return _problem(directory, f"{PRESET_FILE_NAME} 解析失败：{exc}")

    unknown = sorted(set(raw) - _KNOWN_KEYS)
    if unknown:
        return _problem(directory, f"出现未知字段：{', '.join(unknown)}")

    title = raw.get("title")
    if title is not None and not isinstance(title, str):
        return _problem(directory, f"title 必须是字符串，实际：{type(title).__name__}")
    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        return _problem(directory, f"description 必须是字符串，实际：{type(description).__name__}")

    skills, problem = _parse_skills(raw.get("skills"), directory)
    if problem is not None:
        return problem

    return SkillPreset(
        preset_id=preset_id,
        # WHY 标题缺省用 ID 而不是空串：界面上出现一个空白的场景名，用户无法判断它是哪一个。
        title=(title or "").strip() or preset_id,
        description=(description or "").strip(),
        skills=skills,
    )


def load_presets(root: Path) -> PresetCatalog:
    """扫描 ``root`` 下的一级子目录，把每个含 ``preset.toml`` 的目录读成一个场景。

    Args:
        root: 预设根目录（``<应用目录>/skills/presets``）；不存在时返回空结果。

    Returns:
        巡检结果；**任何单个场景的问题都不会让整次巡检失败**（与技能巡检同一口径：
        坏场景被跳过，好场景照常可用）。

    Raises:
        ValueError: ``root`` 为 ``None``。
    """
    if root is None:
        raise ValueError("root 不能为 None：预设目录由 config 给出")

    if not root.is_dir():
        # WHY 不报错：预设是可选能力（与技能库同一口径），缺失只应降级为「没有场景可选」。
        logger.debug("预设目录不存在，跳过：%s", root)
        return PresetCatalog()

    try:
        candidates = sorted(child for child in root.iterdir() if child.is_dir())
    except OSError as exc:
        logger.error("读取预设目录失败：%s（%s）", root, exc)
        return PresetCatalog(problems=(PresetProblem(directory=str(root), reason=f"读取失败：{exc}"),))

    presets: list[SkillPreset] = []
    problems: list[PresetProblem] = []
    for directory in candidates:
        loaded = _load_one(directory)
        if isinstance(loaded, PresetProblem):
            problems.append(loaded)
        else:
            presets.append(loaded)

    # WHY 打 DEBUG 而不是 INFO：本函数由面板与视图重建按需调用（"每次重读"是刻意设计），
    # INFO 会把「刷新一次面板」变成一条日志。真正的装配事实由 SkillService 在构造时记一次。
    logger.debug(
        "场景预设已加载：目录=%s 可用=%d 不可用=%d（%s）",
        root,
        len(presets),
        len(problems),
        ", ".join(preset.preset_id for preset in presets) or "无",
    )
    return PresetCatalog(presets=tuple(presets), problems=tuple(problems))


__all__ = [
    "PRESET_FILE_NAME",
    "PresetCatalog",
    "PresetProblem",
    "SkillPreset",
    "load_presets",
]
