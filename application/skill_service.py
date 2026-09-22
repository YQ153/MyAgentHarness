"""技能库服务：技能清单、启停，以及物化视图的重建。

把三块拼起来（各自的职责边界见其模块 docstring）：

- ``runtime.skills``：技能包**是什么**（解析与校验，诊断取自上游并加严）。
- ``runtime.skill_store``：技能**是否启用**（唯一真相）。
- ``runtime.skill_view``：把「启用中的技能」落成上游能读的一份目录（派生产物）。

两条不变量：

1. **库是真相，视图是派生**。因此状态先落库、再重建视图；重建失败时状态已记录，
   视图停在上一次的样子，而启动时的 ``refresh_view`` 会让它收敛。反过来（先建视图再
   落库）会在写库失败时留下一个「视图里有、库里没有」的技能，而那种不一致没人能解释。
2. **启停只对之后新建的会话生效**。技能索引由上游在**每个会话的第一次运行**时加载一次
   并写进会话状态，已存在的会话沿用其已加载的那份。这不是缺陷而是上游的设计（避免每轮
   重新读取），但它必须写在用户能看到的地方。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from application.errors import NotFoundError
from application.ports import SkillState
from config import VIRTUAL_BUILTIN_SKILLS, VIRTUAL_PRESET_SKILLS, VIRTUAL_SKILLS
from runtime.skill_presets import PresetCatalog, SkillPreset, load_presets
from runtime.skill_store import DEFAULT_ENABLED, GLOBAL_SCOPE
from runtime.skill_view import ViewEntry, ViewResult, rebuild_view, sources_for_graph
from runtime.skills import inspect_skills

if TYPE_CHECKING:
    from config import AppConfig, SessionRoot

logger = logging.getLogger(__name__)

CATEGORY_GENERAL = "general"
CATEGORY_PRESET = "preset"
CATEGORY_USER = "user"
CATEGORY_CUSTOM = "custom"
"""技能分类（界面据此区分「产品自带 / 场景预设 / 我自己放的 / 显式配置的目录」）。

WHY 由**来源虚拟路径**推导，而不是给技能包加一个字段：分类是「它从哪来」这一事实，而
这件事只有来源知道。让技能包自己声明类别，等于允许「放在预置目录里、自称是用户技能」
这类自相矛盾，而面板与 Agent 会各信一边。
"""


def list_presets(config: AppConfig) -> PresetCatalog:
    """列出全部场景预设（**不需要会话上下文**）。

    WHY 单独一个模块级函数：场景清单是"产品交付了哪些场景"，全局一份，与会话/工作区无关
    （新建会话时要先能选场景，要求先有会话就变成循环依赖）。而它又必须经应用层暴露——
    接口层按分层契约不能直接 import ``runtime``；这里是那条依赖的唯一出口。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    return load_presets(config.skill_presets_dir)


class SkillService:
    """技能库的读与启停，以及场景（预设）过滤。"""

    def __init__(self, config: AppConfig, *, scope: SessionRoot, store: SkillState) -> None:
        """构造服务。

        Args:
            config: 应用配置，提供技能目录列表与启停口径。
            scope: 本实例服务的会话根；**必填**。物化视图（挂载为 ``/.skills-active``）
                是按根各一份的衍生物，它在**根外存储**里（``scope.skill_view_store``），
                而建图时 Agent 读的正是本根的那一份。**场景也从它读**（``scope.preset``）：
                场景决定视图里放哪些技能，而视图按根一份，所以场景只能与根绑定。
            store: 启停状态存储。

        Raises:
            ValueError: 任一必需依赖为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if scope is None:
            raise ValueError("scope 不能为 None：物化视图建在哪个存储目录由它决定")
        if store is None:
            raise ValueError("store 不能为 None：启停状态的唯一真相在库里")

        self._config = config
        self._scope = scope
        self._store = store
        self._view_dir = scope.skill_view_store
        logger.info(
            "SkillService 就绪：root=%s preset=%s 视图=%s",
            scope.root,
            scope.preset or "(不限定)",
            self._view_dir,
        )

    # ------------------------------------------------------------------ 场景

    def presets(self) -> PresetCatalog:
        """加载场景预设清单（**每次重读**，与技能库一样支持运行期新增场景）。

        WHY 不缓存在实例上：面板刷一次就重读一次，代价是几个小文件的 TOML 解析；换来的是
        「新建一个场景目录 → 刷新面板就能选」这种一致性。视图侧的重建本来就要重扫技能库，
        两者的时效口径因此保持一致。
        """
        return load_presets(self._config.skill_presets_dir)

    def preset(self) -> SkillPreset | None:
        """本根绑定的场景；未绑定、或场景已不存在时返回 ``None``（按「不限定」处理）。"""
        return self._resolve_preset(self.presets())

    def _resolve_preset(self, catalog: PresetCatalog) -> SkillPreset | None:
        """从已加载的清单里取出本根的场景（附「场景不存在」的告警）。

        WHY 场景不存在时不报错：场景 ID 是**历史会话**里记着的东西，而场景目录可能被删或
        改名。此时让这条会话打不开，比「按不限定继续用」糟糕得多——后者至少还能工作，并且
        日志与面板都会写明发生了什么。
        """
        preset = catalog.get(self._scope.preset)
        if self._scope.preset and preset is None:
            logger.warning(
                "根绑定的场景预设不存在，按「不限定」处理：root=%s preset=%r 可用场景=%s",
                self._scope.root,
                self._scope.preset,
                catalog.ids,
            )
        return preset

    def _category_of(self, source: str) -> str:
        """由来源虚拟路径判断技能分类（见模块头的四个 ``CATEGORY_*`` 常量）。"""
        normalized = source.rstrip("/")
        if normalized == VIRTUAL_BUILTIN_SKILLS:
            return CATEGORY_GENERAL
        # 预设来源是"前缀 + 场景 ID"（每个场景一个目录），所以按前缀判断而不是等值比较。
        if normalized == VIRTUAL_PRESET_SKILLS or normalized.startswith(f"{VIRTUAL_PRESET_SKILLS}/"):
            return CATEGORY_PRESET
        if normalized == VIRTUAL_SKILLS:
            return CATEGORY_USER
        return CATEGORY_CUSTOM

    # ------------------------------------------------------------------ 来源

    @property
    def sources(self) -> list[str]:
        """本根生效的技能来源目录（虚拟路径）。"""
        return list(self._scope.skill_source_paths())

    def view_path(self) -> Path:
        """物化视图目录的绝对路径（根外存储里，由 ``/`` 挂载暴露给 Agent）。"""
        return self._view_dir

    def graph_sources(self) -> tuple[list[str], str]:
        """返回建图应使用的技能来源与告警原因（供能力公示与排错）。"""
        return sources_for_graph(self._view_dir, self.sources, self._scope.skill_view_virtual)

    # ------------------------------------------------------------------ 视图

    async def refresh_view(self, *, state_scope: str = GLOBAL_SCOPE) -> ViewResult:
        """按当前**场景**与启用状态重建物化视图。

        WHY 由服务层统一入口而不是让调用方自己拼三步：顺序错了会造出一份与库里状态
        不符的视图，而它不会报错——下一次会话就是按那份错误视图加载技能的。

        WHY 场景过滤发生在这里、而不是在来源列表上：场景白名单里的技能名可能分散在
        三个来源（通用 / 场景预设 / 用户库），而来源是"整目录"粒度的——只有把三处的
        技能先全部发现、再按名字筛，才能表达「这个场景要这几项，无论它们来自哪里」。

        Args:
            state_scope: 启停状态的**作用域**（默认全局），与工作区是两个维度：
                工作区决定「有哪些技能包可用」，启停作用域决定「其中哪些被选中」；
                场景则决定「其中哪些属于本次任务的场景」。

        Returns:
            重建结果。

        Raises:
            ValueError: ``state_scope`` 非法。
            OSError: 复制或替换失败（此时旧视图仍完整）。
        """
        inventory, enabled = await self._snapshot(state_scope=state_scope)
        preset = self.preset()
        # WHY 只求一次来源表：``skill_host_dir`` 每次调用都会重扫一遍技能目录，而
        # 「有几个技能就扫几遍」在技能多时纯属浪费 IO。
        sources = self._scope.skill_sources()
        entries: list[ViewEntry] = []
        for package in inventory.packages:
            # WHY 场景过滤排在启停之前：两者都满足才进视图，而这个顺序只影响日志归因
            # ——「不在本场景内」与「被用户停用」是两件不同的事，排查时不能混为一谈。
            if preset is not None and not preset.allows(package.name):
                continue
            if not enabled.get(package.name, DEFAULT_ENABLED):
                continue
            # WHY 走作用域反解而不是与工作区拼接：内置技能随应用交付、位于工作区之外，
            # 拼接会得到一个不存在的路径——表现为「技能在清单里、却怎么也复制不进
            # 视图」，而空视图会让它静默失效。
            source_dir = self._scope.skill_host_dir(package.directory, sources=sources)
            if source_dir is None:
                logger.warning(
                    "技能 %s 的来源目录无法确定，本次跳过：%s", package.name, package.directory
                )
                continue
            entries.append(ViewEntry(name=package.name, source_dir=source_dir))
        result = await asyncio.to_thread(rebuild_view, self._view_dir, entries)
        logger.info(
            "技能视图已重建：view=%s preset=%s 启用 %d，移除 %d，跳过 %d",
            self._view_dir,
            self._scope.preset or "(不限定)",
            len(result.copied),
            len(result.removed),
            len(result.skipped),
        )
        return result

    # ------------------------------------------------------------------ 读

    async def list_skills(self, *, state_scope: str = GLOBAL_SCOPE) -> dict[str, Any]:
        """列出技能、启用状态与诊断。

        Returns:
            含 ``items`` / ``unloadable`` / ``load_errors`` / ``view_path`` /
            ``graph_sources`` / ``view_warning`` 的结果字典。

        Raises:
            ValueError: ``state_scope`` 非法。
        """
        inventory, enabled = await self._snapshot(state_scope=state_scope)
        sources, warning = self.graph_sources()
        catalog = self.presets()
        preset = self._resolve_preset(catalog)
        # WHY 单独算出「白名单里有、但当前来源里找不到」的技能名：场景里的名字写对了而技能
        # 包没交付（或名字打错）时，视图会安静地少一项——那正是「我选了场景，Agent 却不会
        # 那项技能」这种无从解释的现象。这里把它变成一条可读的清单。
        known = {package.name for package in inventory.packages}
        missing = sorted(
            name for name in (preset.skills if preset is not None else ()) if name not in known
        )

        return {
            # WHY 响应字段仍叫 ``scope`` 而参数改叫 ``state_scope``：前者是既有 API 契约
            # （面板读它），后者是为了不与「工作区作用域」混名。两者是同一个值的两种叫法。
            "scope": state_scope,
            "preset": (
                {
                    "id": preset.preset_id,
                    "title": preset.title,
                    "description": preset.description,
                    "skills": list(preset.skills),
                }
                if preset is not None
                else None
            ),
            "preset_id": self._scope.preset,
            # WHY 把「没能加载的场景目录」也下发：与技能侧的 ``unloadable`` 同一理由——一个
            # 写坏的 preset.toml 只会让该场景从下拉里消失，没有任何提示。
            "preset_problems": [
                {"directory": item.directory, "reason": item.reason} for item in catalog.problems
            ],
            "missing_skills": missing,
            "items": [
                {
                    "name": package.name,
                    "description": package.description,
                    "directory": package.directory,
                    "skill_md_path": package.skill_md_path,
                    "source": package.source,
                    "category": self._category_of(package.source),
                    "enabled": enabled.get(package.name, DEFAULT_ENABLED),
                    # WHY 由服务端算 ``in_preset`` 而不是让前端比对白名单：清单可能来自不同
                    # 场景的会话，而「属不属于本场景」是本次请求上下文里的判定结果。
                    "in_preset": preset is None or preset.allows(package.name),
                    "problems": list(package.problems),
                }
                for package in inventory.packages
            ],
            # WHY 把「没能加载的候选」一并下发：上游对单个技能的解析失败只写日志，
            # 不放进它的返回值。不在这里报出来，用户看到的就是「我明明建了它，面板里
            # 却没有」，且没有任何可查的线索。
            "unloadable": [
                {"directory": item.directory, "reason": item.reason}
                for item in inventory.unloadable
            ],
            "load_errors": list(inventory.load_errors),
            "view_path": str(self.view_path()),
            "view_exists": self.view_path().is_dir(),
            "graph_sources": sources,
            "view_warning": warning,
        }

    # ------------------------------------------------------------------ 写

    async def set_enabled(
        self, name: str, enabled: bool, *, state_scope: str = GLOBAL_SCOPE
    ) -> dict[str, Any]:
        """启停一个技能，并重建视图。

        WHY 拒绝不存在的技能名（而不是照写一条记录）：技能名多来自界面或模型生成，
        写错一个字母会留下一条永不生效的「ghost 记录」——用户以为自己停用了它，而实际
        什么都没发生，且库里那条记录看起来完全正常。

        Args:
            name: 技能名。
            enabled: ``True`` 启用、``False`` 停用。
            state_scope: 启停状态的作用域（默认全局），与工作区是两个维度。

        Returns:
            写入后的状态（``name`` / ``enabled`` / ``scope`` / ``updated_at``），
            并附上本次视图重建的摘要。

        Raises:
            NotFoundError: 该名字不在已解析的技能清单里。
            ValueError: 参数非法。
            OSError: 视图重建失败（此时状态已落库，下次 ``refresh_view`` 会收敛）。
        """
        inventory, _ = await self._snapshot(state_scope=state_scope)
        known = {package.name for package in inventory.packages}
        if name not in known:
            raise NotFoundError("技能", name)

        record = await self._store.set_enabled(name, enabled, scope=state_scope)
        result = await self.refresh_view(state_scope=state_scope)
        # WHY 显式重打包而不是 ``{**record}``：store 的记录用 ``skill_name``（那是它的列名），
        # 而清单与接口里的字段是 ``name``。直接展开会把列名泄进 API，同一个东西在「清单」
        # 与「启停」两处叫不同名字——调用方迟早按错的那个去写代码。翻译只此一处。
        return {
            "name": record["skill_name"],
            "enabled": record["enabled"],
            "scope": record["scope"],
            "updated_at": record.get("updated_at", ""),
            "view": {
                "path": str(result.view_path),
                "enabled_skills": list(result.copied),
                "skipped": list(result.skipped),
            },
        }

    # ------------------------------------------------------------------ 内部

    async def _snapshot(self, *, state_scope: str):
        """一次取齐「技能清单」与「启用状态」。

        WHY 合成一处：两者必须来自同一次观察。分开取的话，中间若有人改了技能目录，
        就可能出现「按 A 次清单算启用集、按 B 次清单建视图」——产出的视图里会多出或
        少掉一个技能，而两次调用各自看都没问题。

        Raises:
            ValueError: ``state_scope`` 非法。
        """
        inventory = await asyncio.to_thread(
            inspect_skills,
            # WHY 传会话根而不是技能库目录：巡检用 ``mounts`` 解析每个来源的真实位置，
            # 这个参数只是「既不在 mounts 里、又像虚拟路径」时的回落基准（见
            # ``runtime.skills._host_dir_of``）——技能来源现在全都在挂载表里。
            self._scope.root,
            self.sources,
            mounts=self._scope.mount_table,
        )
        enabled = await self._store.resolve(inventory.names, scope=state_scope)
        return inventory, enabled
