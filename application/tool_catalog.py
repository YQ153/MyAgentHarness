"""工具目录服务：对外回答「本进程有哪些工具、它们来自哪里」。

职责边界：只读地呈现装配结果，不做加载、不碰网络。

WHY 需要一个应用层服务而不是让路由直接读 ``ToolBundle``：工具集属于
``agent`` 层的装配产物，接口层不得越过 ``application`` 直接依赖它
（分层契约）；同时审计也需要「工具名 → 来源服务器」的查询，这份逻辑
放在服务里才能被两处复用。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from application.dto import MCPServerInfo, ToolInfo, ToolListResult

if TYPE_CHECKING:
    from agent.tooling import ToolBundle

logger = logging.getLogger(__name__)

_BUILTIN_ORDER: tuple[str, ...] = (
    "write_todos",
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "execute",
    "task",
)
"""内置工具的展示顺序。

WHY 固定顺序而不是用集合的迭代序：集合序依赖插入历史，接口每重启一次
返回顺序就可能变一次，前端列表会无意义地跳动，也让「对比两次启动的
工具清单」变得困难。此处未列出的内置工具按字典序追加在后面。
"""


class ToolCatalog:
    """工具清单的只读视图。"""

    def __init__(self, bundle: ToolBundle) -> None:
        """构造目录。

        Args:
            bundle: 装配层产出的工具集；``None`` 时按「无扩展工具」处理。

        Raises:
            ValueError: ``bundle`` 为 ``None``。
        """
        if bundle is None:
            raise ValueError("bundle 不能为 None")

        self._bundle = bundle
        logger.info(
            "工具目录就绪：扩展 %d 个，自定义模块 %d 个，MCP 服务器 %d 台",
            len(bundle.descriptors),
            len(bundle.custom_modules),
            len(bundle.mcp_statuses),
        )

    def list_tools(self) -> ToolListResult:
        """列出全部生效工具（内置 + 扩展）及其来源。

        Returns:
            工具清单；扩展工具按注册顺序排在内置工具之后。
        """
        items: list[ToolInfo] = [
            ToolInfo(name=name, source="builtin") for name in self._builtin_names()
        ]
        for descriptor in self._bundle.descriptors:
            items.append(
                ToolInfo(
                    name=descriptor.name,
                    source=descriptor.source.value,
                    description=descriptor.description,
                    server=descriptor.server,
                )
            )

        servers = [
            MCPServerInfo(
                name=status.name,
                transport=status.transport,
                ok=status.ok,
                tool_count=status.tool_count,
                error=status.error,
            )
            for status in self._bundle.mcp_statuses
        ]

        return ToolListResult(
            items=items,
            total=len(items),
            custom_modules=list(self._bundle.custom_modules),
            mcp_servers=servers,
        )

    def server_of(self, tool_name: str) -> str | None:
        """返回工具所属的 MCP 服务器名；非 MCP 工具返回 ``None``。

        WHY 供审计复用而不是让审计自己查目录：工具归属的判定规则
        （前缀匹配 / 单服务器唯一确定）只有一份定义，重复实现会出现
        「接口显示来自 A，审计记成 B」的不一致。
        """
        return self._bundle.server_of(tool_name)

    def source_of(self, tool_name: str) -> str:
        """返回工具来源：``builtin`` / ``custom`` / ``mcp`` / ``unknown``。

        WHY 需要 ``unknown`` 这个取值：工具名可能来自旧版本配置或已被停用的
        server，把它硬归到 ``builtin`` 会让审计读起来像「这是内置能力」，
        而归到 ``custom`` 又会冤枉自定义模块；如实标注未知才是正确归因。
        """
        for descriptor in self._bundle.descriptors:
            if descriptor.name == tool_name:
                return descriptor.source.value
        if tool_name in self._bundle.builtin_names:
            return "builtin"
        return "unknown"

    def _builtin_names(self) -> list[str]:
        """返回按固定顺序排列的内置工具名。

        WHY 从装配产物取而不是回查内核常量表（``agent.tools.BUILTIN_TOOL_NAMES``）：
        「本轮装配了哪些内置工具」本身就是装配结果的一部分，``ToolBundle``
        已经携带它。应用层因此只依赖"装配产物"这一个概念，不必知道内核把
        内置工具名放在哪里——依赖内核实现细节会让内核的调整波及应用层。
        """
        known = set(self._bundle.builtin_names)
        ordered = [name for name in _BUILTIN_ORDER if name in known]
        ordered.extend(sorted(known - set(ordered)))
        return ordered

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志排错
        return f"ToolCatalog(extensions={len(self._bundle.descriptors)})"


__all__ = ["ToolCatalog"]
