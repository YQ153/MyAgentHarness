"""技能物化视图：把"启用中的技能"落成一份可被上游读取的目录。

WHY 需要它（探针实测，``scripts/probe_skills.py``）：``SkillsMiddleware`` 的 ``sources``
必须是**技能目录的父目录**，把来源直接指向某个技能目录（``sources=['/skills/code-review']``）
会**一个都加载不到，且没有任何告警**。所以「按启用状态决定加载哪些技能」不能靠来源列表
过滤，只能派生出这样一份目录——库里是真相，视图是产物，技能包本体永远不动。

WHY 视图用**固定路径**：图的装配会缓存，而缓存下来的图持有的是来源路径。若视图路径随
每次重建而变化，缓存的图就会一直指向旧路径（表现为「改了启停却不生效」）。路径固定后，
内容变化会在**新会话**读取技能时自然生效——技能索引本就每会话加载一次。

WHY 目录名以点开头：它是程序生成的产物、不是用户的资料，必须被工作区文件面板与知识库的
目录遍历跳过；否则用户会看到一份自己没建过、改了也不生效的「技能副本」。

WHY 先建到临时目录再替换：重建中途失败若留下**半个视图**，后果是部分技能静默消失。
先建全再换，失败时旧视图仍然完整。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

VIEW_DIR_NAME = ".skills-active"
"""视图目录名（工作区内的一级目录）；建图时作为唯一来源。"""

VIEW_TEMP_SUFFIX = ".building"
"""构建中的临时目录后缀；与视图目录同处一层，保证替换是同卷 rename。"""

_SKILL_MARKER = "SKILL.md"
"""判定一个目录是不是技能包的定义文件（与 Agent Skills 规范一致）。"""


@dataclass(frozen=True)
class ViewEntry:
    """一个应出现在视图里的技能。

    Attributes:
        name: 技能名，同时充当视图里的目录名。
        source_dir: 技能包本体所在目录（宿主机绝对路径）。
    """

    name: str
    source_dir: Path


@dataclass(frozen=True)
class ViewResult:
    """一次重建的结果。

    Attributes:
        view_path: 视图目录的绝对路径。
        copied: 本次复制进视图的技能名。
        removed: 上一版视图里有、本次不再需要的技能名。
        skipped: 因源目录缺失或不是技能包而未复制进去的技能名。
    """

    view_path: Path
    copied: tuple[str, ...]
    removed: tuple[str, ...]
    skipped: tuple[str, ...]


def view_directory(workspace_root: Path) -> Path:
    """返回视图目录的绝对路径（不保证存在）。"""
    return Path(workspace_root) / VIEW_DIR_NAME


def view_source_path() -> str:
    """返回视图目录作为**后端虚拟路径**的写法（``sources`` 用的就是它）。"""
    return "/" + VIEW_DIR_NAME


def validate_entry_name(name: str) -> str:
    """校验技能名可以安全地当作一个目录名使用。

    WHY 这一层必须自己判（而不是信任上游）：技能名会成为视图里的**目录名**，而目录名
    参与路径拼接。含分隔符或 ``..`` 的名字能把它写到视图之外——这是 T23 安全红线在
    文件系统侧的入口，与「技能脚本执行沿用沙箱审批链」是同一件事的两个面。

    Raises:
        ValueError: 为空、非字符串、含路径分隔符或为上跳片段。
    """
    if not isinstance(name, str):
        raise ValueError(f"技能名必须是字符串，实际：{type(name).__name__}")
    candidate = name.strip()
    if not candidate:
        raise ValueError("技能名不能为空")
    if candidate in {".", ".."} or "/" in candidate or "\\" in candidate:
        raise ValueError(f"技能名不能作为目录名使用：{name!r}")
    return candidate


def _copy_skill(source_dir: Path, target_dir: Path) -> None:
    """把一个技能包整目录复制到视图里。

    WHY 整目录而不是只复制 ``SKILL.md``：技能正文常引用同目录下的辅助脚本，而模型是按
    **视图里的路径**去读它们的（探针已确认软链下辅助文件可读，复制同理）。只复制定义
    文件会让那些引用在运行时指向不存在的路径。
    """
    shutil.copytree(source_dir, target_dir)


def rebuild_view(workspace_root: Path, entries: Sequence[ViewEntry]) -> ViewResult:
    """按给定技能集重建视图，并返回本次变化。

    这是一个**全量替换**：视图最终等同于 ``entries`` 描述的那一份技能集。增量更新需要
    判断「哪些旧目录还在新集合里」，而那个判断一旦出错留下的就是永不消失的幽灵技能。

    Args:
        workspace_root: 工作区根目录。
        entries: 应出现在视图里的技能。**名字必须唯一**——重名会让一个技能覆盖另一个，
            而覆盖是静默的。

    Returns:
        重建结果。

    Raises:
        ValueError: 技能名重复，或名字不能作为目录名使用。
        OSError: 复制或替换失败（此时旧视图仍然完整，见模块 docstring）。
        RuntimeError: 替换阶段的不变量被破坏。
    """
    root = Path(workspace_root)
    view = view_directory(root)
    temp = root / (VIEW_DIR_NAME + VIEW_TEMP_SUFFIX)

    seen: set[str] = set()
    for entry in entries:
        name = validate_entry_name(entry.name)
        if name in seen:
            raise ValueError(f"技能名重复：{name}（视图里一个目录只能对应一个技能）")
        seen.add(name)

    previous = _existing_skill_names(view)

    # WHY 先清掉遗留的构建目录：上一次构建中途崩溃（如进程被杀）会把它留在那里，
    # 而它带着半份内容——下次直接复用会得到一个「看起来建好了」的错误视图。
    if temp.exists():
        shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped: list[str] = []
    for entry in entries:
        name = validate_entry_name(entry.name)
        source = Path(entry.source_dir)
        if not (source / _SKILL_MARKER).is_file():
            # WHY 跳过而不是让整次重建失败：单个技能包被删除或损坏，不该让其余技能的
            # 启停一起失灵。调用方从 skipped 里能看到发生了什么。
            logger.warning("技能源目录不是有效技能包，已跳过：%s", source)
            skipped.append(name)
            continue
        _copy_skill(source, temp / name)
        copied.append(name)

    if view.exists():
        shutil.rmtree(view)
    temp.rename(view)

    if not view.is_dir():
        # 走到这里说明替换阶段没把目录放到位，而后续所有会话都会因此读不到技能——
        # 必须炸，不能让它表现为「技能突然全没了」。
        raise RuntimeError(f"技能视图替换失败：{view} 不存在")

    removed = tuple(sorted(previous - set(copied)))
    logger.info(
        "技能视图已重建：%s（复制 %d，移除 %d，跳过 %d）",
        view,
        len(copied),
        len(removed),
        len(skipped),
    )
    return ViewResult(
        view_path=view, copied=tuple(sorted(copied)), removed=removed, skipped=tuple(skipped)
    )


def _existing_skill_names(view: Path) -> set[str]:
    """列出当前视图里的技能目录名；视图不存在时返回空集合。"""
    if not view.is_dir():
        return set()
    try:
        return {child.name for child in view.iterdir() if child.is_dir()}
    except OSError:
        logger.debug("列举既有技能视图失败，按空处理：%s", view)
        return set()


def discard_view(workspace_root: Path) -> None:
    """删除视图（含构建中的临时目录）；不存在时静默通过。"""
    root = Path(workspace_root)
    for target in (view_directory(root), root / (VIEW_DIR_NAME + VIEW_TEMP_SUFFIX)):
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)


def sources_for_graph(
    workspace_root: Path, configured_sources: Sequence[str]
) -> tuple[list[str], str]:
    """决定建图时该用哪份技能来源。

    WHY 需要这个判定（fail-loud）：视图是派生产物，它可能因为程序升级、磁盘被清理、
    或重建失败而不存在。此时若把「视图不存在」当成「没有技能」，全部技能会**静默消失**
    ——用户只会看到 Agent 突然不会那些套路了。因此这里退回**用户配置的真实目录**（等于
    全部启用），并把原因回给调用方去告警。

    Args:
        workspace_root: 工作区根目录。
        configured_sources: 用户配置的技能来源（虚拟路径）。

    Returns:
        ``(建图应使用的来源列表, 需要告警的原因)``；原因为空串表示一切正常。
    """
    configured = [source for source in configured_sources if str(source).strip()]
    if view_directory(workspace_root).is_dir():
        return [view_source_path()], ""
    if not configured:
        return [], ""
    return configured, (
        f"技能视图 {view_directory(workspace_root)} 不存在，已退回配置的技能目录"
        "（此时所有技能都处于启用状态）；请检查启停视图是否被清理或重建失败"
    )
