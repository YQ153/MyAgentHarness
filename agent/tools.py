"""工具注册器：自定义工具与 MCP 工具的唯一入口。

职责边界：只回答「本进程一共有哪些工具、它们各自来自哪里」，不负责建连
（``agent.mcp`` 的事），也不负责把它们交给图（``agent.graph`` 的事）。

WHY 需要「注册」而不是直接拼一个 list 传给 ``create_deep_agent``：内置工具
名是模型可见的全局命名空间，一个重名的自定义工具会静默顶掉内置工具——
模型以为自己在写文件，实际调用的是另一个实现。注册器把这类冲突在装配期
就变成一条明确的错误。
"""

from __future__ import annotations

import importlib
import inspect
import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """工具装配错误的基类。"""


class ToolNameConflictError(ToolError):
    """工具名冲突：与内置工具或已注册工具同名。

    WHY 单独定义异常类型：冲突是「配置错误」，调用方需要把它与「外部服务
    不可用」区分开——前者必须人工改配置，后者可以重试或降级。
    """


class CustomToolModuleError(ToolError):
    """自定义工具模块无法加载。"""


class ToolSource(StrEnum):
    """工具的来源。

    WHY 需要来源：排查「Agent 为什么会调用这个工具」时，第一件事就是确认
    它是不是我们自己装的；审计里同样要能区分「内置文件操作」与「第三方
    MCP server 提供的能力」，二者风险量级不同。
    """

    BUILTIN = "builtin"
    CUSTOM = "custom"
    MCP = "mcp"


BUILTIN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
        "task",
        "write_todos",
    }
)
"""deepagents 内置工具名。

WHY 显式列出而不是运行时从图里反射：反射要在图装配之后才能拿到工具集，
而冲突必须在装配之前判定——那时还没有图。这里承担「内置名空间」的
事实来源，升级 deepagents 时是本文件唯一需要同步的地方。
"""


@dataclass(frozen=True)
class ToolDescriptor:
    """一个已注册工具的可序列化描述（不含可执行对象）。"""

    name: str
    source: ToolSource
    description: str = ""
    server: str | None = None
    """来源 MCP 服务器名；非 MCP 工具为 ``None``。"""


def _coerce_tool(
    tool: BaseTool | Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
) -> BaseTool:
    """把「工具或可调用对象」统一成 ``BaseTool``。

    Args:
        tool: ``BaseTool`` 实例或普通函数。
        name: 覆盖工具名；``None`` 时取工具自身的名字。
        description: 覆盖描述；``None`` 时取工具自身的描述。

    Returns:
        规范化后的工具。

    Raises:
        ValueError: ``tool`` 为 ``None`` 或类型既非 ``BaseTool`` 也不可调用。
    """
    if tool is None:
        raise ValueError("tool 不能为 None")

    if isinstance(tool, BaseTool):
        return tool

    if not callable(tool):
        raise ValueError(f"工具必须是 BaseTool 或可调用对象，实际：{type(tool).__name__}")

    # WHY 用 StructuredTool.from_function：函数的类型注解就是参数 schema，
    # 让自定义工具与内置工具拥有同一套参数校验与错误提示，模型侧无需区分。
    return StructuredTool.from_function(
        func=tool,
        name=name,
        description=description,
        infer_schema=True,
    )


class ToolRegistry:
    """按名字去重地装配工具，并记录每个工具的来源。

    并发安全：注册动作可能在装配期由多个工作线程触发（例如 CLI 与后台
    任务同时首次取图），注册表用一把互斥锁保护内部字典——锁内不做任何
    可能阻塞的 IO，临界区只有几次字典写入。
    """

    def __init__(self, *, builtin_names: Iterable[str] = BUILTIN_TOOL_NAMES) -> None:
        """构造注册器。

        Args:
            builtin_names: 内置工具名空间；自定义工具与之同名时报错。

        Raises:
            ValueError: ``builtin_names`` 为 ``None``。
        """
        if builtin_names is None:
            raise ValueError("builtin_names 不能为 None")

        self._builtin_names: frozenset[str] = frozenset(builtin_names)
        self._tools: dict[str, BaseTool] = {}
        self._descriptors: dict[str, ToolDescriptor] = {}
        self._lock = threading.Lock()

    def register(
        self,
        tool: BaseTool | Callable[..., Any],
        *,
        source: ToolSource = ToolSource.CUSTOM,
        server: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> BaseTool:
        """注册一个工具。

        Args:
            tool: ``BaseTool`` 实例或可调用对象。
            source: 工具来源。
            server: 来源 MCP 服务器名；``MCP`` 来源必填，其余为 ``None``。
            name: 覆盖工具名（仅对可调用对象生效）。
            description: 覆盖描述（仅对可调用对象生效）。

        Returns:
            已注册的工具实例。

        Raises:
            ValueError: ``tool`` 非法、``source`` 非法或 ``server`` 缺失。
            ToolNameConflictError: 与内置工具或已注册工具同名。
        """
        if not isinstance(source, ToolSource):
            raise ValueError(f"source 必须是 ToolSource，实际：{type(source).__name__}")
        if source is ToolSource.MCP and not (isinstance(server, str) and server.strip()):
            raise ValueError("MCP 来源的工具必须提供 server 名称")

        resolved = _coerce_tool(tool, name=name, description=description)
        tool_name = (resolved.name or "").strip()
        if not tool_name:
            raise ValueError("工具名不能为空")
        if not isinstance(server, str) or not server.strip():
            server = None

        with self._lock:
            if tool_name in self._builtin_names:
                raise ToolNameConflictError(
                    f"工具名 {tool_name!r} 与内置工具冲突：自定义工具不能覆盖内置工具，"
                    f"请改名（内置工具：{sorted(self._builtin_names)}）"
                )
            existing = self._descriptors.get(tool_name)
            if existing is not None:
                raise ToolNameConflictError(
                    f"工具名 {tool_name!r} 已注册（来源={existing.source.value}"
                    f"{'' if existing.server is None else f'，服务器={existing.server}'}），"
                    f"请改名或为 MCP 服务器开启名称前缀（mcp_tool_name_prefix）"
                )

            self._tools[tool_name] = resolved
            self._descriptors[tool_name] = ToolDescriptor(
                name=tool_name,
                source=source,
                description=(resolved.description or "").strip(),
                server=server.strip() if isinstance(server, str) else None,
            )

        logger.info(
            "已注册工具：name=%s source=%s server=%s",
            tool_name,
            source.value,
            server or "-",
        )
        return resolved

    def register_tool(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        source: ToolSource = ToolSource.CUSTOM,
        server: str | None = None,
    ) -> Callable[[Callable[..., Any]], BaseTool]:
        """装饰器形式的注册入口。

        WHY 提供装饰器：自定义工具最常见的写法就是一个带类型注解的函数，
        装饰器让「定义」与「注册」写在同一处，不会出现「函数改了但忘记
        加进 TOOLS」这类漂移。

        Args:
            name: 覆盖工具名。
            description: 覆盖描述。
            source: 工具来源。
            server: 来源 MCP 服务器名。

        Returns:
            装饰器；被装饰函数会被注册并返回 ``BaseTool``。
        """

        def decorator(func: Callable[..., Any]) -> BaseTool:
            return self.register(
                func,
                source=source,
                server=server,
                name=name,
                description=description,
            )

        return decorator

    def names(self) -> list[str]:
        """已注册工具名（按注册顺序）。"""
        with self._lock:
            return list(self._tools)

    def tools(self) -> list[BaseTool]:
        """已注册工具实例（按注册顺序）。"""
        with self._lock:
            return list(self._tools.values())

    def descriptors(self) -> list[ToolDescriptor]:
        """已注册工具的描述（按注册顺序）。"""
        with self._lock:
            return list(self._descriptors.values())

    def __len__(self) -> int:
        """已注册工具数量。"""
        with self._lock:
            return len(self._tools)

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志排错
        return f"ToolRegistry(tools={len(self)})"


def _accepts_config(hook: Callable[..., Any]) -> bool:
    """判断注册钩子是否接受第二个位置参数（``config``）。

    WHY 按签名判断而不是「先按两参调用、失败了再退回一参」：后者会把钩子
    内部真实的 ``TypeError`` 误判成「这个钩子只收一个参数」，于是错误被
    吞掉、工具静默缺失——这正是注册器要消灭的那类失败。

    Args:
        hook: 模块提供的 ``register_tools`` 可调用对象。

    Returns:
        ``True`` 表示应传入 ``config``。
    """
    try:
        parameters = list(inspect.signature(hook).parameters.values())
    except (TypeError, ValueError):
        # 少数内建/扩展对象没有可读签名；保守按「不收 config」处理，
        # 与本次扩展之前的行为一致，不会把原本能跑的模块变成加载失败。
        return False

    positional = [
        item
        for item in parameters
        if item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 2:
        return True
    return any(item.kind is item.VAR_POSITIONAL for item in parameters)


def load_custom_tool_modules(
    modules: Sequence[str],
    registry: ToolRegistry,
    *,
    config: Any = None,
) -> list[str]:
    """按点分路径导入自定义工具模块并注册其中的工具。

    模块需提供下列二者之一：
    - ``TOOLS``：工具或可调用对象的可迭代对象；
    - ``register_tools(registry, config)``：自行调用注册器的函数。
      第二个参数可省略（写成 ``register_tools(registry)``），此时不传配置。

    Args:
        modules: 模块点分路径列表。
        registry: 目标注册器。
        config: 应用配置；仅当钩子声明了第二个位置参数时传入。

    Returns:
        成功加载的模块名列表。

    Raises:
        ValueError: ``registry`` 为 ``None``。
        CustomToolModuleError: 模块导入失败、或模块既无 ``TOOLS`` 也无
            ``register_tools``。

    WHY 导入失败直接抛出而不是跳过：模块名写错、依赖未装这类问题只会
    表现为「Agent 少了某个能力」，而少了哪个能力往往要等到某次对话失败
    才被发现；在启动期失败反而最便宜。

    WHY 要能把配置传进去（2026-09-18 扩展）：此前的入口只给注册器，而
    ``.env`` 里的值只进配置对象、不进 ``os.environ``，于是「需要密钥或阈值
    的模块」根本读不到自己的配置——只能各自去读环境变量或重解析 ``.env``，
    两条路都会绕开 ``config.py`` 这个唯一解析点。触发这个缺口的首个用例是
    联网工具：它既要求「配置进 config.py」，又要求「密钥缺失时不注册」，
    而这两个要求都以「模块能看到配置」为前提。

    WHY 用签名判断而不是固定传两参：既有模块写的是
    ``register_tools(registry)``，多传一个参数会让它们全部变成加载失败——
    扩展点必须加法演进，不能要求所有已部署的模块跟着改。
    """
    if registry is None:
        raise ValueError("registry 不能为 None")

    loaded: list[str] = []
    for dotted in modules or []:
        if not isinstance(dotted, str) or not dotted.strip():
            raise CustomToolModuleError(f"模块名必须是非空字符串，实际：{dotted!r}")

        try:
            module = importlib.import_module(dotted.strip())
        except Exception as exc:
            raise CustomToolModuleError(f"自定义工具模块导入失败：{dotted}（{exc}）") from exc

        before = len(registry)
        register_hook = getattr(module, "register_tools", None)
        declared = getattr(module, "TOOLS", None)

        if callable(register_hook):
            if _accepts_config(register_hook):
                # WHY 记录「拿到了配置」：模块按条件不注册任何工具是合法结果
                # （缺密钥就该缺席），此时新增 0 个工具是预期而非故障，日志要
                # 能区分这两种情况。
                logger.debug("自定义工具模块 %s 接受配置参数", dotted)
                register_hook(registry, config)
            else:
                register_hook(registry)
        elif isinstance(declared, Iterable):
            for item in declared:
                registry.register(item, source=ToolSource.CUSTOM)
        else:
            raise CustomToolModuleError(
                f"自定义工具模块 {dotted} 既未提供 TOOLS，也未提供 register_tools(registry)"
            )

        added = len(registry) - before
        logger.info("已加载自定义工具模块：%s（新增 %d 个工具）", dotted, added)
        loaded.append(dotted.strip())

    return loaded


__all__ = [
    "BUILTIN_TOOL_NAMES",
    "CustomToolModuleError",
    "ToolDescriptor",
    "ToolError",
    "ToolNameConflictError",
    "ToolRegistry",
    "ToolSource",
    "load_custom_tool_modules",
]
