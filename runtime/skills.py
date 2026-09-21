"""技能包解析与校验：列出技能目录里的技能，并给出上游不会报的那类问题。

WHY 用上游的 ``SkillsMiddleware`` 做解析，而不是自己写一份 frontmatter 解析：

- 上游**没有公开的解析入口**（解析藏在中间件内部），能用的就是「给它一个 backend 与
  来源列表，让它加载一次」。
- 自己写一份的代价不是多几十行代码，而是**两份格式方言**：上游改了字段语义、或
  Agent Skills 规范调了约束，我们这份会静默停在旧口径上，表现为「面板显示正常、Agent
  却加载不出来」——而这类偏差没有任何报错。
- 因此这里的做法是：**解析交给上游，诊断加在我们这一层**。

WHY 还要加自己的校验：探针实测（``scripts/probe_skills.py``）上游对**违反命名规范**的
技能**只告警、仍然加载**。于是工作区里会出现「我把 name 改了，行为却没变」这种无从解释
的现象。本模块把这类情况提升为**显式问题**（``SkillPackage.problems``），由面板照实展示。

约束常量直接复用上游导出的 ``MAX_SKILL_*``：自己写 64 / 1024 这些数字，等于给规范留一份
随时会漂移的副本。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.middleware.skills import (
    MAX_SKILL_DESCRIPTION_LENGTH,
    MAX_SKILL_NAME_LENGTH,
    SkillsMiddleware,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

_SKILL_FILE = "SKILL.md"
"""技能包的定义文件；与 Agent Skills 规范一致。"""


@dataclass(frozen=True)
class SkillPackage:
    """一个已解析的技能包。

    Attributes:
        name: frontmatter 里的技能标识。
        description: 供模型判断「这个技能适不适用」的说明。
        directory: 技能目录的虚拟路径（相对工作区），如 ``/skills/code-review``。
        skill_md_path: ``SKILL.md`` 的虚拟路径，即模型读取完整指令的位置。
        source: 发现它的来源目录。
        problems: 上游能加载、但**我们认为有问题**的地方；空元组表示无异议。
    """

    name: str
    description: str
    directory: str
    skill_md_path: str
    source: str
    problems: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class UnloadableSkill:
    """一个**没能被加载**的候选技能目录。

    WHY 需要这个类型（这是实测逼出来的）：上游对「缺 ``description``」「无 frontmatter」
    这类单个技能的解析失败**只写日志**，并把技能直接丢掉——它的 ``skills_load_errors``
    只承载**来源级**失败。于是只依赖上游返回值的话，这些技能会在清单里彻底消失，用户
    看到的是「我明明建了这个技能，面板里却没有」，且没有任何可查的线索。

    Attributes:
        directory: 候选技能目录的虚拟路径。
        reason: 为什么它没被加载（**启发式**判断，权威判定仍在上游）。
    """

    directory: str
    reason: str


@dataclass(frozen=True)
class SkillInventory:
    """一次技能目录巡检的结果。

    Attributes:
        packages: 已解析的技能包（上游已按「后面的来源覆盖前面的」去重）。
        load_errors: 上游报出的**来源级**加载问题。
        unloadable: 候选目录里没能被加载的那些，附带原因。
        sources: 本次巡检实际使用的来源目录（空白项已被剔除）。
    """

    packages: tuple[SkillPackage, ...]
    load_errors: tuple[str, ...]
    sources: tuple[str, ...]
    unloadable: tuple[UnloadableSkill, ...] = ()

    @property
    def names(self) -> list[str]:
        """全部技能名（已排序，便于稳定断言与展示）。"""
        return sorted(package.name for package in self.packages)


def _directory_of(skill_md_path: str) -> str:
    """从 ``.../SKILL.md`` 取它所在的技能目录。

    WHY 不用字符串切割：路径可能带尾斜杠、也可能因后端实现而多一层分隔符；
    ``PurePosixPath`` 的 ``parent`` 表达的是同一个语义，且不会在这些细节上分叉。
    """
    parent = PurePosixPath(skill_md_path).parent
    return "/" + str(parent).lstrip("/") if str(parent) != "." else ""


def _name_problem(name: str) -> str:
    """按 Agent Skills 规范校验技能名；返回空串表示合规。

    WHY 镜像上游的 Unicode 判定而不是写 ASCII 正则：上游允许 ``café`` 这类带音标的
    小写名字（``isalpha() and islower()``）。用 ``[a-z0-9-]`` 判会把它判为非法，而
    它在上游是能正常加载的——那种「我们的面板说不行、Agent 却用得好好的」分歧，
    比不校验更让人困惑。
    """
    if not name:
        return "缺少 name（frontmatter 必须提供）"
    if len(name) > MAX_SKILL_NAME_LENGTH:
        return f"name 超过 {MAX_SKILL_NAME_LENGTH} 字符（当前 {len(name)}）"
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return "name 只能含单个连字符，且不能以连字符开头或结尾"
    for char in name:
        if char == "-":
            continue
        if not (char.isdigit() or (char.isalpha() and char.islower())):
            return f"name 含非法字符：{char!r}（只允许小写字母、数字与连字符）"
    return ""


def _validate(name: str, directory: str, description: str) -> tuple[str, ...]:
    """收集「上游能加载、但不合规范」的问题。

    Returns:
        问题描述列表；空元组表示无异议。
    """
    problems: list[str] = []

    name_problem = _name_problem(name)
    if name_problem:
        problems.append(name_problem)

    # WHY 这条单独拎出来（上游只给告警）：规范要求 name 与所在目录同名，不同名时技能
    # 仍会被加载——于是「改了 frontmatter 的 name 却不生效」「按目录名去搜技能搜不到」
    # 都会发生，而面板若照实显示 name，用户完全看不出问题在哪。
    folder = PurePosixPath(directory).name
    if name and folder and name != folder:
        problems.append(
            f"name（{name}）与所在目录名（{folder}）不一致；"
            "上游只告警不拒绝，但按目录名找不到它会让人以为是加载失败"
        )

    if not description:
        problems.append("缺少 description（模型靠它判断这个技能适不适用）")
    elif len(description) > MAX_SKILL_DESCRIPTION_LENGTH:
        problems.append(
            f"description 超过 {MAX_SKILL_DESCRIPTION_LENGTH} 字符（当前 {len(description)}）"
        )

    return tuple(problems)


def inspect_skills(
    workspace_root: Path,
    sources: Sequence[str],
    *,
    backend: object | None = None,
    mounts: Mapping[str, Path] | None = None,
) -> SkillInventory:
    """列出来源目录下的技能包并给出诊断。

    Args:
        workspace_root: 工作区根目录；工作区内的来源以相对它的虚拟路径表示。
        sources: 来源目录（如 ``["/skills"]``）。空列表直接返回空结果——不构造 backend。
        backend: 复用的 backend；``None`` 表示自建一个只读的文件系统视图。
        mounts: 「虚拟路径前缀 → 宿主机目录」的挂载表，用于工作区**之外**的来源
            （随应用交付的内置技能就是这种）。缺了它，区外来源会一个都读不到，
            且没有任何告警。

    Returns:
        巡检结果；**任何单个技能的问题都不会让整次巡检失败**（与上游一致：
        坏技能被跳过，好技能照常列出）。

    Note:
        这是同步函数：上游的 ``before_agent`` 是同步的，包装成协程只会让调用方
        以为底层发生了 IO 等待。调用方若在异步上下文里，用 ``asyncio.to_thread``。
    """
    normalized = _normalize_sources(sources)
    if not normalized:
        return SkillInventory(packages=(), load_errors=(), sources=())

    view = backend if backend is not None else _build_view(workspace_root, mounts)
    middleware = SkillsMiddleware(backend=view, sources=list(normalized))  # type: ignore[arg-type]

    # WHY 直接调 before_agent：它是中间件真正「读一次技能」的入口，且返回结构里就带着
    # 元数据与告警。RUN/STATE 参数在上游当前实现里未被使用（源码标注 unused），因此
    # 传空值即可；这条依赖由一个用例钉住，上游若改了行为会红在测试上，而不是静默空列表。
    update = middleware.before_agent({}, None, {})  # type: ignore[arg-type]

    raw_metadata = list((update or {}).get("skills_metadata", []))
    load_errors = tuple(str(item) for item in (update or {}).get("skills_load_errors", []))

    packages: list[SkillPackage] = []
    for item in raw_metadata:
        name = str(item.get("name", ""))
        description = str(item.get("description", "") or "")
        skill_md_path = str(item.get("path", ""))
        directory = _directory_of(skill_md_path)
        packages.append(
            SkillPackage(
                name=name,
                description=description,
                directory=directory,
                skill_md_path=skill_md_path,
                source=_source_of(skill_md_path, normalized),
                problems=_validate(name, directory, description),
            )
        )

    # WHY 还要自己扫一遍候选目录做差集：上游对单个技能的解析失败只写日志、不放进返回值，
    # 只依赖它的输出会让「建了技能却不在清单里」变成一个没有线索的现象。差集能保证
    # **每一个被丢掉的候选都被报出来**。
    loaded_directories = {package.directory for package in packages}
    unloadable = tuple(
        UnloadableSkill(directory=virtual, reason=_diagnose_reason(host_dir))
        for virtual, host_dir in _candidate_directories(workspace_root, normalized, mounts)
        if virtual not in loaded_directories
    )

    logger.debug(
        "技能巡检完成：来源=%s 已加载=%d 未能加载=%d 来源级告警=%d",
        list(normalized),
        len(packages),
        len(unloadable),
        len(load_errors),
    )
    return SkillInventory(
        packages=tuple(packages),
        load_errors=load_errors,
        sources=normalized,
        unloadable=unloadable,
    )


def _normalize_sources(sources: Sequence[str]) -> tuple[str, ...]:
    """归一来源目录：去空白、补前导斜杠、剔除空项。

    WHY 必须剔除空串（实测）：给上游传一个空来源会让**整次加载一件都拿不到**——不报错、
    也没有告警，表现为「所有技能凭空消失」。一个来自配置尾部分隔符的空项不该有这个后果。
    """
    normalized: list[str] = []
    for source in sources:
        if not isinstance(source, str):
            continue
        candidate = source.strip()
        if not candidate:
            continue
        candidate = "/" + candidate.strip("/")
        if candidate != "/" and candidate not in normalized:
            normalized.append(candidate)
    return tuple(normalized)


def _build_view(
    workspace_root: Path, mounts: Mapping[str, Path] | None
) -> FilesystemBackend | CompositeBackend:
    """构造技能巡检用的只读虚拟视图（工作区默认根 + 区外来源的挂载路由）。

    WHY 区外来源必须挂载：backend 的根是工作区，而随应用交付的内置技能在工作区
    之外。挂上虚拟路径后，巡检与上游加载用的是**同一套路径语义**；不挂的话它一个
    都读不到，且没有任何告警——表现为「内置技能装了却用不上」。
    """
    default = FilesystemBackend(root_dir=workspace_root, virtual_mode=True)
    if not mounts:
        return default
    return CompositeBackend(
        default=default,
        routes={
            prefix: FilesystemBackend(root_dir=host_dir, virtual_mode=True)
            for prefix, host_dir in mounts.items()
        },
    )


def _host_dir_of(
    workspace_root: Path, source: str, mounts: Mapping[str, Path] | None
) -> Path | None:
    """把一个来源虚拟路径还原成宿主机目录；不属于任何来源时返回 ``None``。

    WHY 取最长匹配而不是第一个命中：来源可以嵌套（``/skills`` 与 ``/skills/team``），
    取短的会把团队技能当成基础目录下的技能，而它的真实位置不是那里。
    """
    normalized = "/" + source.strip("/")
    best_length = -1
    best: Path | None = None
    for prefix, host_dir in (mounts or {}).items():
        virtual = "/" + prefix.strip("/")
        if normalized == virtual:
            candidate = host_dir
        elif normalized.startswith(virtual + "/"):
            candidate = host_dir / normalized[len(virtual) + 1 :]
        else:
            continue
        if len(virtual) > best_length:
            best_length = len(virtual)
            best = candidate
    if best is not None:
        return best
    if not normalized.strip("/"):
        return None
    return workspace_root / normalized.strip("/")


def _candidate_directories(
    workspace_root: Path, sources: Sequence[str], mounts: Mapping[str, Path] | None
) -> list[tuple[str, Path]]:
    """列出「看起来是技能目录」的候选（含 SKILL.md 的一级子目录）。

    WHY 走真实文件系统而不是 backend：候选枚举只是拿来做差集，技能包本就是真实文件；
    为此再抽象一层「列出远端存储的一级子目录」没有收益。真正决定「能不能加载」的仍是上游。

    WHY 同时返回虚拟路径与宿主机目录：虚拟路径是给用户看的（与清单里其余路径同一口径），
    宿主机目录是诊断读 ``SKILL.md`` 时用的——两者在区外来源上不再相等，只回其中一个
    都会在另一处再算一遍。

    Returns:
        ``(虚拟路径, 宿主机目录)`` 列表；来源目录不存在时该来源整体跳过。
    """
    candidates: list[tuple[str, Path]] = []
    for source in sources:
        base = _host_dir_of(workspace_root, source, mounts)
        if base is None:
            continue
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir() or not (child / _SKILL_FILE).is_file():
                    continue
            except OSError:
                continue
            candidates.append((f"{source.rstrip('/')}/{child.name}", child))
    return candidates


def _diagnose_reason(skill_dir: Path) -> str:
    """给出「这个候选为什么没被加载」的**启发式**原因。

    WHY 只做文本级判断、不解析 YAML：权威的加载判定在上游，这里只负责把最常见的原因
    说清楚（缺哪一项）。若在这一层也做一遍解析，就等于有了两份格式方言——而不论哪一份
    先过时，症状都是「面板说没问题、Agent 却加载不了」。
    """
    try:
        text = (skill_dir / _SKILL_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"读取 SKILL.md 失败：{type(exc).__name__}"

    keys = _frontmatter_keys(text)
    if keys is None:
        return "缺少 YAML frontmatter（文件需以单独一行的 --- 开始并以 --- 结束）"

    missing = [key for key in ("name", "description") if key not in keys]
    if missing:
        return f"frontmatter 缺少必需项：{'、'.join(missing)}"
    return "上游未加载（原因只在日志里；常见于 frontmatter 不是合法 YAML）"


def _frontmatter_keys(text: str) -> set[str] | None:
    """取 frontmatter 里出现过的顶层键名；没有 frontmatter 时返回 ``None``。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    keys: set[str] = set()
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            return keys
        if ":" in line and not line[:1].isspace():
            keys.add(line.split(":", 1)[0].strip().lower())
    return None


def _source_of(skill_md_path: str, sources: Sequence[str]) -> str:
    """判断某个技能来自哪个来源目录。

    WHY 取**最长匹配**而不是第一个匹配：来源可以嵌套（``/skills`` 与 ``/skills/team``），
    取第一个会把 ``/skills/team/x`` 报成来自 ``/skills``，而那正好掩盖了「团队技能覆盖了
    基础技能」这条最该被看见的信息。
    """
    normalized = "/" + skill_md_path.lstrip("/")
    candidates = [
        source for source in sources if normalized.startswith("/" + source.strip("/") + "/")
    ]
    if not candidates:
        return ""
    return max(candidates, key=len)
