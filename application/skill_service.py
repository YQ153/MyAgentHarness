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
from runtime.skill_store import DEFAULT_ENABLED, GLOBAL_SCOPE, SkillStateStore
from runtime.skill_view import rebuild_view, sources_for_graph, view_directory, ViewEntry, ViewResult
from runtime.skills import inspect_skills

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)


class SkillService:
    """技能库的读与启停。"""

    def __init__(self, config: AppConfig, *, store: SkillStateStore) -> None:
        """构造服务。

        Args:
            config: 应用配置，提供工作区根目录与技能目录列表。
            store: 启停状态存储。

        Raises:
            ValueError: 任一必需依赖为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if store is None:
            raise ValueError("store 不能为 None：启停状态的唯一真相在库里")

        self._config = config
        self._store = store
        self._root = Path(config.workspace)

    # ------------------------------------------------------------------ 来源

    @property
    def sources(self) -> list[str]:
        """当前配置的技能来源目录（虚拟路径）。"""
        return list(self._config.skill_source_paths())

    def view_path(self) -> Path:
        """物化视图目录的绝对路径。"""
        return view_directory(self._root)

    def graph_sources(self) -> tuple[list[str], str]:
        """返回建图应使用的技能来源与告警原因（供能力公示与排错）。"""
        return sources_for_graph(self._root, self.sources)

    # ------------------------------------------------------------------ 视图

    async def refresh_view(self, *, scope: str = GLOBAL_SCOPE) -> ViewResult:
        """按当前启用状态重建物化视图。

        WHY 由服务层统一入口而不是让调用方自己拼三步：顺序错了会造出一份与库里状态
        不符的视图，而它不会报错——下一次会话就是按那份错误视图加载技能的。

        Args:
            scope: 作用域，默认全局。

        Returns:
            重建结果。

        Raises:
            ValueError: ``scope`` 非法。
            OSError: 复制或替换失败（此时旧视图仍完整）。
        """
        inventory, enabled = await self._snapshot(scope=scope)
        entries = [
            ViewEntry(name=package.name, source_dir=self._root / package.directory.lstrip("/"))
            for package in inventory.packages
            if enabled.get(package.name, DEFAULT_ENABLED)
        ]
        result = await asyncio.to_thread(rebuild_view, self._root, entries)
        logger.info(
            "技能视图已重建：启用 %d，移除 %d，跳过 %d",
            len(result.copied),
            len(result.removed),
            len(result.skipped),
        )
        return result

    # ------------------------------------------------------------------ 读

    async def list_skills(self, *, scope: str = GLOBAL_SCOPE) -> dict[str, Any]:
        """列出技能、启用状态与诊断。

        Returns:
            含 ``items`` / ``unloadable`` / ``load_errors`` / ``view_path`` /
            ``graph_sources`` / ``view_warning`` 的结果字典。

        Raises:
            ValueError: ``scope`` 非法。
        """
        inventory, enabled = await self._snapshot(scope=scope)
        sources, warning = self.graph_sources()

        return {
            "scope": scope,
            "items": [
                {
                    "name": package.name,
                    "description": package.description,
                    "directory": package.directory,
                    "skill_md_path": package.skill_md_path,
                    "source": package.source,
                    "enabled": enabled.get(package.name, DEFAULT_ENABLED),
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
        self, name: str, enabled: bool, *, scope: str = GLOBAL_SCOPE
    ) -> dict[str, Any]:
        """启停一个技能，并重建视图。

        WHY 拒绝不存在的技能名（而不是照写一条记录）：技能名多来自界面或模型生成，
        写错一个字母会留下一条永不生效的「ghost 记录」——用户以为自己停用了它，而实际
        什么都没发生，且库里那条记录看起来完全正常。

        Args:
            name: 技能名。
            enabled: ``True`` 启用、``False`` 停用。
            scope: 作用域，默认全局。

        Returns:
            写入后的状态（``name`` / ``enabled`` / ``scope`` / ``updated_at``），
            并附上本次视图重建的摘要。

        Raises:
            NotFoundError: 该名字不在已解析的技能清单里。
            ValueError: 参数非法。
            OSError: 视图重建失败（此时状态已落库，下次 ``refresh_view`` 会收敛）。
        """
        inventory, _ = await self._snapshot(scope=scope)
        known = {package.name for package in inventory.packages}
        if name not in known:
            raise NotFoundError("技能", name)

        record = await self._store.set_enabled(name, enabled, scope=scope)
        result = await self.refresh_view(scope=scope)
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

    async def _snapshot(self, *, scope: str):
        """一次取齐「技能清单」与「启用状态」。

        WHY 合成一处：两者必须来自同一次观察。分开取的话，中间若有人改了技能目录，
        就可能出现「按 A 次清单算启用集、按 B 次清单建视图」——产出的视图里会多出或
        少掉一个技能，而两次调用各自看都没问题。

        Raises:
            ValueError: ``scope`` 非法。
        """
        inventory = await asyncio.to_thread(
            inspect_skills, self._root, self.sources
        )
        enabled = await self._store.resolve(inventory.names, scope=scope)
        return inventory, enabled
