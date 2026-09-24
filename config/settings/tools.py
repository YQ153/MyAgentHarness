"""工具扩展域配置：自定义工具模块清单、MCP 服务器清单与审计开关。

字段从原 ``config.AppConfig`` 的「工具扩展（自定义工具 / MCP）」分区
（原 L804–869）与 ``MCPServerSpec`` 模型（原 L305–369）整体迁入。
``MCPServerSpec`` 与 ``mcp_servers`` 字段强耦合（前者是后者的元素类型），
二者必须同处一个文件，否则会出现「改了 spec 忘了看字段」的分叉。
"""

from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import NoDecode

from config.enums import MCPTransport
from config.parsing import parse_list_config


class MCPServerSpec(BaseModel):
    """单个 MCP 服务器的连接描述。

    WHY 用模型而不是裸 dict：MCP 连接参数是最容易写错的一类配置（命令与
    URL 二选一、headers/env 类型不同），用模型可以在加载期就给出「哪个字段
    缺了」的精确报错，而不是等到建连超时才看到一条与配置无关的异常。

    Attributes:
        name: 服务器在本系统内的唯一标识，用于日志、审计与工具归属。
        transport: 传输方式。
        enabled: ``False`` 时保留配置但不建连——排查某个 server 的最快方式
            是单独停掉它，而不是把整段配置删掉后再凭记忆补回。
        command: ``stdio`` 传输下的可执行文件。
        args: ``stdio`` 传输下的命令行参数。
        env: ``stdio`` 传输下传给子进程的环境变量；**不继承宿主环境**，
            避免把宿主机的 ``*_API_KEY`` 一并泄漏给第三方 MCP server。
        cwd: ``stdio`` 子进程的工作目录。
        url: ``sse`` / ``streamable_http`` / ``websocket`` 传输下的服务地址。
        headers: HTTP 系传输的附加请求头（常用于承载鉴权令牌）。
    """

    model_config = {"extra": "forbid"}
    """拼错的字段名直接报错，而不是被静默忽略后表现为「配置没生效」。"""

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    transport: MCPTransport = MCPTransport.STDIO
    enabled: bool = True
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("transport", mode="before")
    @classmethod
    def _normalize_transport(cls, value: object) -> object:
        """容错大小写与空白；与 ``execution_mode`` 保持同一口径。"""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @model_validator(mode="after")
    def _validate_transport(self) -> MCPServerSpec:
        """按传输方式校验必需字段。

        Raises:
            ValueError: ``stdio`` 缺 ``command``，或 URL 类传输缺 ``url`` /
                地址格式不对。
        """
        if self.transport is MCPTransport.STDIO:
            if not self.command or not self.command.strip():
                raise ValueError(f"MCP 服务器 {self.name}：stdio 传输必须提供 command")
            return self

        if not self.url or not self.url.strip():
            raise ValueError(f"MCP 服务器 {self.name}：{self.transport.value} 传输必须提供 url")
        scheme = self.url.split("://", 1)[0].lower()
        expected = ("ws://", "wss://") if self.transport is MCPTransport.WEBSOCKET else ("http://", "https://")
        if not self.url.startswith(expected):
            raise ValueError(
                f"MCP 服务器 {self.name}：{self.transport.value} 传输的 url 必须以 "
                f"{' 或 '.join(expected)} 开头，实际：{scheme}://"
            )
        return self


class ToolsSettings(BaseModel):
    """工具扩展域的字段：自定义工具模块、MCP 清单与审计开关。"""

    custom_tool_modules: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """需要加载的自定义工具模块（点分路径）。

    模块需提供 ``TOOLS``（工具或可调用对象列表）或 ``register_tools(registry)``
    二者之一；钩子也可以声明第二个参数 ``register_tools(registry, config)``，
    此时会拿到本配置对象——需要按配置决定「注册哪些工具、用什么阈值」的模块
    必须用这种写法（``.env`` 里的值只进配置对象、不进 ``os.environ``，模块自己
    去读环境变量是读不到的）。
    WHY 走配置而不是在内核里 import：新增一个工具不应该修改内核
    代码，否则「工具集」就成了和内核同样的变更风险等级。

    WHY 加载失败要直接报错（而非跳过）：模块名写错与工具缺失一样，都会被
    用户感知为「Agent 突然不会做某件事了」，静默跳过只会让排查从加载期
    推迟到某次具体对话失败时。

    环境变量支持两种写法：逗号分隔（``CUSTOM_TOOL_MODULES=a.b,c.d``）或
    JSON 数组（``CUSTOM_TOOL_MODULES=["a.b","c.d"]``）。
    """

    mcp_enabled: bool = True
    """MCP 总开关。

    WHY 单独留一个总开关：逐个把 server 的 ``enabled`` 置为 False 需要改
    N 处，而排障时最常见的需求正是「先整体关掉外部工具」。未配置任何
    server 时，本开关无论取何值都不会产生连接。
    """

    mcp_servers: list[MCPServerSpec] = Field(default_factory=list)
    """MCP 服务器清单；环境变量写法为 JSON 数组（``MCP_SERVERS=[{...}]``）。

    WHY 不把连接参数硬编码进代码：MCP server 是部署环境相关的外部进程，
    本地与服务器上的命令路径、鉴权头都不一样；硬编码会让同一份代码在
    两个环境里只有一个能跑。
    """

    mcp_tool_name_prefix: bool = True
    """是否为 MCP 工具名加上 ``服务器名_`` 前缀。

    WHY 默认开启：两个 server 提供同名工具时，后加载者会覆盖前者，而模型
    只看到一个工具——这种「装了两个实际只生效一个」的失败没有任何报错。
    加前缀后冲突在注册期就会显式暴露。
    """

    mcp_load_timeout_seconds: int = Field(default=15, ge=1)
    """单个 MCP server 拉取工具清单的超时秒数。

    WHY 必须有：stdio 型 server 启动失败时往往既不退出也不响应握手，
    没有超时会让整个装配流程永久挂起，表现为「服务起不来但没有报错」。
    """

    mcp_fail_fast: bool = False
    """某个 MCP server 加载失败时是否阻断启动。

    WHY 默认不阻断：MCP server 多为第三方进程，让它成为本服务的可用性
    单点并不划算；降级时仍会写 ERROR 日志并在 ``/api/tools`` 里暴露失败
    状态，属于「显式降级」而非静默吞错。
    """

    tool_audit_builtin: bool = False
    """是否把内置工具（读写文件、执行命令等）的调用也写入审计。

    WHY 默认关闭：内置工具在一次多步任务里可能被调用几十次，全量落库会
    让审计表体积随对话量线性膨胀，反而冲淡真正需要留痕的扩展工具调用；
    而内置命令执行本身已由 HITL 审批留痕。排障内置工具行为时可临时打开。
    """

    @field_validator("custom_tool_modules", mode="before")
    @classmethod
    def _parse_tool_modules(cls, value: object) -> object:
        """把配置里的模块清单解析成列表，口径见 ``parse_list_config``。

        点分模块名不含逗号，因此用逗号分隔（与 ``SANDBOX_ENV_ALLOWLIST`` 同一
        口径）；JSON 数组写法同样接受。
        """
        return parse_list_config(value, field="custom_tool_modules", separators=(",",))

    @field_validator("custom_tool_modules", mode="after")
    @classmethod
    def _strip_tool_modules(cls, value: list[str]) -> list[str]:
        """去掉空白项与多余空格。

        WHY 空串要剔除：``CUSTOM_TOOL_MODULES=""`` 在 shell 里解出一个空串，
        当模块名去 import 只会得到一条与用户意图无关的 ImportError。

        非字符串项由 ``list[str]`` 的元素校验拦下（本方法之前的注解校验），
        因此这里只需处理字符串的空白——若在此再判一次类型，那行代码永远
        不可达，反而让人误以为有两条防线。
        """
        return [item.strip() for item in value if item.strip()]

    @field_validator("mcp_servers", mode="after")
    @classmethod
    def _validate_mcp_server_names(cls, value: list[MCPServerSpec]) -> list[MCPServerSpec]:
        """服务器名必须唯一。

        WHY 在加载期就判重：名字同时是日志标识、审计字段与工具前缀来源，
        重名会让「这条工具来自哪个 server」这个问题失去答案。
        """
        seen: set[str] = set()
        for spec in value:
            if spec.name in seen:
                raise ValueError(f"MCP 服务器名重复：{spec.name}")
            seen.add(spec.name)
        return value
