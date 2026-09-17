"""MCP（Model Context Protocol）服务器接入。

职责边界：把配置里的服务器清单变成一批 LangChain 工具，并如实报告每台
服务器的加载结果；不负责判重（``agent.tools.ToolRegistry`` 的事）。

WHY 逐个服务器单独拉取工具：``MultiServerMCPClient.get_tools()`` 一次性
并发拉取全部服务器，且不做异常隔离——任一台失败会让其余服务器的工具
一起丢失，还会把失败原因埋进一条聚合异常里。逐台拉取 + 逐台记录状态，
才能保证「一个第三方进程挂了」不会影响到其它能力。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from config import MCPTransport

if TYPE_CHECKING:
    from config import AppConfig, MCPServerSpec

logger = logging.getLogger(__name__)


class MCPLoadError(Exception):
    """MCP 工具加载失败且配置为 fail-fast 时抛出。"""


@dataclass(frozen=True)
class MCPServerStatus:
    """一台 MCP 服务器的加载结果。

    WHY 用状态对象而不是只回工具列表：运维需要回答「某个工具为什么没出现」，
    而工具列表本身答不了这个问题——缺失的工具根本不在列表里。
    """

    name: str
    transport: str
    ok: bool
    tool_count: int = 0
    error: str = ""

    @property
    def state(self) -> str:
        """用于日志与接口的简短状态。"""
        return "ok" if self.ok else "failed"


@dataclass(frozen=True)
class MCPLoadResult:
    """一次 MCP 加载的整体结果。"""

    tools: tuple[BaseTool, ...] = ()
    statuses: tuple[MCPServerStatus, ...] = ()

    @property
    def failed(self) -> tuple[MCPServerStatus, ...]:
        """加载失败的服务器。"""
        return tuple(item for item in self.statuses if not item.ok)

    @property
    def ok(self) -> bool:
        """是否全部加载成功。"""
        return not self.failed


class MCPToolLoader:
    """按配置加载 MCP 服务器提供的工具。"""

    def __init__(self, config: AppConfig) -> None:
        """构造加载器。

        Args:
            config: 应用配置，提供服务器清单、超时与降级策略。

        Raises:
            ValueError: ``config`` 为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")

        self._config = config

    def build_connections(self) -> dict[str, Any]:
        """把配置翻译成 ``MultiServerMCPClient`` 需要的连接字典。

        Returns:
            ``{服务器名: 连接配置}``；无待连接服务器时为空字典。

        WHY ``stdio`` 显式传 ``env`` 而不是让它继承：库在 ``env`` 缺省时会把
        当前进程的部分环境变量透传给子进程，其中常含 ``*_API_KEY`` 与云
        凭证；第三方 MCP server 通常不需要它们，泄漏面必须收口。
        """
        connections: dict[str, Any] = {}
        for spec in self._config.active_mcp_servers():
            connections[spec.name] = self._build_connection(spec)
        return connections

    async def load(self) -> MCPLoadResult:
        """拉取所有已启用服务器的工具清单。

        Returns:
            工具与逐台服务器的状态。

        Raises:
            ValueError: 未配置任何待连接的服务器（调用方应先判空）。
            MCPLoadError: 配置了 ``mcp_fail_fast`` 且有服务器加载失败。

        WHY 超时单独包装：stdio 型 server 启动失败时常常既不退出也不响应
        握手，``asyncio.wait_for`` 是唯一能让装配流程继续的手段；超时后
        被取消的任务由库内部的上下文管理器负责回收子进程。
        """
        connections = self.build_connections()
        if not connections:
            return MCPLoadResult()

        client = MultiServerMCPClient(
            connections,
            tool_name_prefix=self._config.mcp_tool_name_prefix,
        )
        # WHY 保留库默认的「执行错误回传模型」行为：MCP 工具执行失败会以
        # status=error 的 ToolMessage 回到模型，运行层据此写审计并让 Agent
        # 自行纠错；若改成抛异常，一次第三方工具抖动就会中断整轮运行。

        statuses: list[MCPServerStatus] = []
        all_tools: list[BaseTool] = []
        failures = 0

        for name in connections:
            spec = self._spec_by_name(name)
            transport = spec.transport.value if spec is not None else "unknown"
            try:
                tools = await asyncio.wait_for(
                    client.get_tools(server_name=name),
                    timeout=self._config.mcp_load_timeout_seconds,
                )
            except asyncio.CancelledError:
                # WHY 取消必须原样上抛：装配被取消意味着进程正在退出，
                # 把它记成「某台服务器失败」会掩盖真正的退出原因。
                raise
            except Exception as exc:
                failures += 1
                logger.error(
                    "MCP 服务器加载失败：name=%s transport=%s error=%s: %s",
                    name,
                    transport,
                    type(exc).__name__,
                    exc,
                )
                statuses.append(
                    MCPServerStatus(
                        name=name,
                        transport=transport,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            all_tools.extend(tools)
            statuses.append(
                MCPServerStatus(
                    name=name,
                    transport=transport,
                    ok=True,
                    tool_count=len(tools),
                )
            )
            logger.info("MCP 服务器已就绪：name=%s transport=%s tools=%d", name, transport, len(tools))

        if failures and self._config.mcp_fail_fast:
            detail = "；".join(f"{item.name}: {item.error}" for item in statuses if not item.ok)
            raise MCPLoadError(f"{failures} 个 MCP 服务器加载失败（mcp_fail_fast=true）：{detail}")

        return MCPLoadResult(tools=tuple(all_tools), statuses=tuple(statuses))

    # ------------------------------------------------------------------ 内部

    def _spec_by_name(self, name: str) -> MCPServerSpec | None:
        """按名字取回服务器配置。"""
        for spec in self._config.active_mcp_servers():
            if spec.name == name:
                return spec
        return None

    def _build_connection(self, spec: MCPServerSpec) -> dict[str, Any]:
        """构造单台服务器的连接配置。

        Raises:
            ValueError: 传输方式不受支持（配置层已校验，此处为防御性分支）。
        """
        if spec.transport is MCPTransport.STDIO:
            return {
                "transport": "stdio",
                "command": (spec.command or "").strip(),
                "args": list(spec.args or []),
                "env": dict(spec.env or {}),
                **({"cwd": spec.cwd} if spec.cwd else {}),
            }

        if spec.transport is MCPTransport.SSE:
            return {
                "transport": "sse",
                "url": (spec.url or "").strip(),
                "headers": dict(spec.headers or {}),
            }

        if spec.transport is MCPTransport.HTTP:
            return {
                "transport": "streamable_http",
                "url": (spec.url or "").strip(),
                "headers": dict(spec.headers or {}),
            }

        if spec.transport is MCPTransport.WEBSOCKET:
            return {
                "transport": "websocket",
                "url": (spec.url or "").strip(),
                "headers": dict(spec.headers or {}),
            }

        raise ValueError(f"不支持的 MCP 传输方式：{spec.transport}")


__all__ = ["MCPLoadError", "MCPLoadResult", "MCPServerStatus", "MCPToolLoader"]
