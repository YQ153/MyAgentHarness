"""工具装配：把自定义工具与 MCP 工具合成一份交给图的工具集。

WHY 单独成模块：``ToolRegistry`` 与 ``MCPToolLoader`` 各自只做一件事，
而「先装自定义的、再装 MCP 的、冲突了怎么办、结果怎么报告」属于装配
顺序与策略，放在任一方都会让它长出第二项职责。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent.mcp import MCPServerStatus, MCPToolLoader
from agent.tools import (
    BUILTIN_TOOL_NAMES,
    ToolDescriptor,
    ToolNameConflictError,
    ToolRegistry,
    ToolSource,
    load_custom_tool_modules,
)
from langchain_core.tools import BaseTool

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolBundle:
    """装配完成的工具集及其可观测元数据。"""

    tools: tuple[BaseTool, ...] = ()
    descriptors: tuple[ToolDescriptor, ...] = ()
    custom_modules: tuple[str, ...] = ()
    mcp_statuses: tuple[MCPServerStatus, ...] = ()
    builtin_names: frozenset[str] = frozenset()
    """本次装配视为"内置"的工具名。

    WHY 随装配结果一起交出：应用层的工具目录需要区分内置 / 自定义 / MCP，
    而"哪些名字是内置的"来自 ``agent.tools.BUILTIN_TOOL_NAMES``——那是内核
    装配细节。让 bundle 携带它，应用层就只依赖"装配产物"这一个概念，
    不必回查内核常量表（见《架构遗留问题治理方案》Q9）。
    """

    @property
    def mcp_enabled(self) -> bool:
        """本次装配是否真的接入了 MCP 服务器。"""
        return bool(self.mcp_statuses)

    def server_of(self, tool_name: str) -> str | None:
        """返回某工具所属的 MCP 服务器名；非 MCP 工具返回 ``None``。

        WHY 需要提供这个查询：审计只拿得到工具名，而「这个调用发往哪个
        第三方服务」正是审计要回答的问题；让调用方自己去遍历描述列表，
        迟早会漏掉某个分支。
        """
        for descriptor in self.descriptors:
            if descriptor.name == tool_name:
                return descriptor.server
        return None


async def build_tool_bundle(config: AppConfig) -> ToolBundle:
    """按配置装配全部扩展工具。

    顺序：自定义模块 → MCP。WHY 自定义优先：它们是本仓库的一等公民，
    MCP 作为外部能力出现冲突时应让位——否则装一个新 server 就可能静默
    顶掉我们自己实现的同名工具。

    Args:
        config: 应用配置。

    Returns:
        工具集与其来源描述。

    Raises:
        ValueError: ``config`` 为 ``None``。
        ToolNameConflictError: 工具名冲突（含 MCP 工具之间的同名冲突）。
    """
    if config is None:
        raise ValueError("config 不能为 None")

    registry = ToolRegistry()

    custom_modules = load_custom_tool_modules(config.custom_tool_modules, registry, config=config)

    mcp_statuses: tuple[MCPServerStatus, ...] = ()
    if config.active_mcp_servers():
        result = await MCPToolLoader(config).load()
        mcp_statuses = result.statuses
        for tool in result.tools:
            # WHY 每条工具单独 try：冲突信息要指出「哪个工具来自哪个
            # 服务器」，整批 try 只能给出第一条，而运维往往要一次性知道
            # 全部冲突项才能改配置。
            try:
                registry.register(
                    tool,
                    source=ToolSource.MCP,
                    server=_server_of_tool(tool, config),
                )
            except ToolNameConflictError as exc:
                raise ToolNameConflictError(f"MCP 工具注册失败：{exc}") from exc

    descriptors = tuple(registry.descriptors())
    logger.info(
        "工具装配完成：总计 %d 个（自定义模块 %d 个，MCP 服务器 %d 台/%d 失败）",
        len(descriptors),
        len(custom_modules),
        len(mcp_statuses),
        sum(1 for item in mcp_statuses if not item.ok),
    )

    return ToolBundle(
        tools=tuple(registry.tools()),
        descriptors=descriptors,
        custom_modules=tuple(custom_modules),
        mcp_statuses=mcp_statuses,
        builtin_names=BUILTIN_TOOL_NAMES,
    )


def _server_of_tool(tool: BaseTool, config: AppConfig) -> str:
    """推断一个 MCP 工具来自哪台服务器。

    WHY 靠名字前缀反推：``langchain-mcp-adapters`` 只在开启前缀时才把
    服务器名写进工具名，关闭前缀时工具的来源信息在工具对象上并不存在。
    开启前缀时用前缀精确匹配，关闭时若只有一台服务器则唯一确定，
    否则退化为 ``"mcp"``——宁可给一个模糊但真实的归属，也不要留空
    （空值会被审计读成「来源未知」，进而误判为内置工具）。
    """
    name = tool.name or ""
    if config.mcp_tool_name_prefix:
        for spec in config.active_mcp_servers():
            prefix = f"{spec.name}_"
            if name.startswith(prefix):
                return spec.name

    active = config.active_mcp_servers()
    if len(active) == 1:
        return active[0].name
    return "mcp"


__all__ = ["ToolBundle", "build_tool_bundle"]
