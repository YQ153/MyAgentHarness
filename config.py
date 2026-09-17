"""统一配置中心。

所有环境变量、路径与运行时参数只在此处声明与校验，避免散落到各个模块后
出现多处重复解析、校验口径不一致的问题。

读取优先级：环境变量 > .env 文件 > 字段默认值。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "OS",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "TZ",
    "LANG",
)
"""沙箱环境变量白名单。

刻意不含 ``USERPROFILE`` / ``HOME`` / ``APPDATA``：这些变量会引导 git、
aws-cli 等工具去读用户目录下的凭据文件，属于「合法变量导致的凭据泄漏」。

WHY 定义在配置层而非 runtime 层：``runtime.sandbox`` 需要引用它构造策略，
而它又是配置项的默认值，放在下层会让 config 反向依赖 runtime。
"""

NETWORK_MODE_NONE = "none"
NETWORK_MODE_HOST = "host"
"""沙箱网络模式；``none`` 表示不向子进程传递代理类变量。"""


class ExecutionMode(StrEnum):
    """``execute`` 工具的执行档位。

    local:    宿主机直跑 shell，仅限本机开发，与 ``LocalShellBackend`` 的安全
              警告一致——绝不可用于 Web 或多租户环境。
    sandbox:  沙箱内执行 shell。具体隔离强度由 ``SandboxTier`` 决定，当前已
              实现 Tier 0（进程沙箱，零依赖），需与人工审批配合使用。
    disabled: 使用非沙盒后端，``execute`` 工具仍存在但调用后返回错误；
              这是默认档位，保证进程上线即处于安全状态。
    """

    LOCAL = "local"
    SANDBOX = "sandbox"
    DISABLED = "disabled"


class SandboxTier(StrEnum):
    """``sandbox`` 档位下的隔离实现档位。

    auto:    按隔离强度从高到低探测，选中首个可用的档位（wsl → process）。
    process: Tier 0——宿主机进程沙箱。Windows 用 Job Object 做进程树管控
             与资源上限，POSIX 用进程组做超时终止。**不是安全边界**，
             必须与 HITL 人工审批配合。
    wsl:     Tier 1——在 WSL2 发行版内执行，与宿主之间隔着 utility VM 边界
             与独立的 Linux 权限模型，资源上限由 Linux rlimit 施加。
             发行版是持久环境（不是一次性容器），且通过 ``/mnt`` 仍能读写
             宿主文件，因此同样需要与人工审批配合。
    docker:  Tier 2——容器内执行。尚未实现。
    """

    AUTO = "auto"
    PROCESS = "process"
    WSL = "wsl"
    DOCKER = "docker"


class MCPTransport(StrEnum):
    """MCP 服务器的传输方式。

    取值与 ``langchain-mcp-adapters`` 的 ``Connection`` 字面量一一对应，
    HTTP 侧用 ``streamable_http`` 而非 ``http``——后者是该库早期版本的别名，
    写成 ``http`` 会在建连阶段才报错，而配置应在加载期就拦下。
    """

    STDIO = "stdio"
    SSE = "sse"
    HTTP = "streamable_http"
    WEBSOCKET = "websocket"


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


def parse_list_config(value: object, *, field: str, separators: tuple[str, ...]) -> object:
    """把「列表型配置」的原始值解析成序列。

    WHY 需要 ``NoDecode`` + 本函数：``pydantic-settings`` 对 ``list[...]`` 这类
    复杂类型默认按 JSON 解码，``SANDBOX_ENV_ALLOWLIST=PATH,TEMP`` 会在加载期
    抛一条与用户意图无关的 JSON 解析错误，而分隔符写法才是 shell 与 ``.env``
    里的常规写法（``PATH`` 本身即如此）。这里同时接受两种形态：以 ``[`` 开头
    按 JSON 解析，否则按分隔符切分。

    WHY 解析权收在本函数而不是每个字段各写一份：四个列表型字段（工具模块、
    MCP 清单、环境白名单、技能目录）需要完全一致的「空值、空白、非法类型」
    口径，各写一份迟早会出现「某个字段把空串当成一个有效项」这类偏差。

    Args:
        value: 原始值，可能是环境变量字符串、已构造好的序列或 ``None``。
        field: 字段名，仅用于错误信息与日志定位。
        separators: 允许的分隔符，**第一个为主分隔符**。普通字符串列表用
            ``,``；路径列表用 ``os.pathsep``（Windows 为 ``;``，POSIX 为 ``:``
            ——路径本身可能含逗号，不能拿逗号当路径分隔符）。

    Returns:
        解析后的列表；``None`` 与空串都归一为空列表，空白项被剔除。
        序列入参原样转成 ``list``，交由字段注解做元素级校验。

    Raises:
        ValueError: ``separators`` 为空；字符串以 ``[`` 开头但不是合法 JSON
            数组；类型既非字符串也非序列。
    """
    if not separators:
        msg = "separators 至少需要一个分隔符"
        logger.error("%s：%s", field, msg)
        raise ValueError(f"{msg}（字段：{field}）")
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                msg = f"{field} 不是合法 JSON 数组：{exc}"
                logger.error("%s", msg)
                raise ValueError(msg) from exc
            logger.debug("配置项 %s 按 JSON 解析出 %d 项", field, len(decoded))
            return decoded
        normalized = text
        for separator in separators[1:]:
            normalized = normalized.replace(separator, separators[0])
        items = [item.strip() for item in normalized.split(separators[0]) if item.strip()]
        logger.debug("配置项 %s 按分隔符 %r 解析出 %d 项", field, separators[0], len(items))
        return items
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    msg = f"{field} 必须是字符串或序列，实际：{type(value).__name__}"
    logger.error("%s", msg)
    raise ValueError(msg)


def is_loopback_host(host: str | None) -> bool:
    """判断监听地址是否只对本机可达。

    WHY 用 ``ipaddress`` 而不是字符串白名单：``127.0.0.53``、``::1`` 与
    ``[::1]`` 都是回环，逐个枚举必然漏；漏掉一个的后果是把本来安全的绑定
    报成「暴露」，而这类误报出现几次之后，告警就会被当成噪音忽略——真正
    危险的那次会一起被忽略。

    Args:
        host: 监听地址或主机名，允许 ``None``（未配置）。

    Returns:
        True 表示可判定为回环（IP 回环段，或 ``localhost``）；
        ``None`` / 空白 / 无法解析的主机名一律返回 False——无法证明是本机时
        按「可能对外」处理，宁可误报不可漏报。

    Raises:
        ValueError: ``host`` 既非 ``None`` 也非字符串。
    """
    if host is None:
        return False
    if not isinstance(host, str):
        msg = f"host 必须是字符串或 None，实际：{type(host).__name__}"
        logger.error("%s", msg)
        raise ValueError(msg)

    candidate = host.strip()
    if not candidate:
        return False
    # ``[::1]`` 是 URL 里的 IPv6 字面量写法，直接交给 ip_address 会解析失败
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1].strip()

    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return candidate.lower() == "localhost"


class AppConfig(BaseSettings):
    """应用配置。

    使用 ``pydantic-settings`` 而非裸 ``os.getenv``：字段的类型转换、
    缺省值与非法值拦截由框架统一处理，调用方拿到的永远是可信对象。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 模型 ----------------
    # 密钥类字段一律 repr=False：AppConfig 对象会出现在 pytest 断言失败输出、
    # 日志与异常上报里，默认 repr 会把明文密钥一并带出去——这条路径不报错、
    # 不告警，泄露发生时没有任何提示（回归见 tests/test_config_repr.py）。
    deepseek_api_key: str = Field(default="", repr=False)
    deepseek_api_base: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    default_model: str = "deepseek-flash"

    openai_api_key: str = Field(default="", repr=False)
    """OpenAI 的 API Key；留空则不注册 ``openai`` 别名。"""

    openai_api_base: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    anthropic_api_key: str = Field(default="", repr=False)
    """Anthropic 的 API Key；留空则不注册 ``anthropic`` 别名。"""

    anthropic_api_base: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-3-5-sonnet-latest"

    ollama_base_url: str = "http://localhost:11434"
    """本地 Ollama 服务地址；仅在显式设置 ``OLLAMA_BASE_URL`` 时注册该别名。"""

    ollama_model: str = "llama3.1"

    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_timeout: float = Field(default=60.0, gt=0.0)
    llm_max_retries: int = Field(default=2, ge=0)

    # ---------------- 运行时路径 ----------------
    workspace: Path = Path("./workspace")
    memory_file: Path = Path("./workspace/AGENTS.md")
    db_path: Path = Field(default=Path("./.data/agent.db"))
    skill_dirs: Annotated[list[Path], NoDecode] = Field(
        default_factory=lambda: [Path("./workspace/skills")]
    )
    """技能目录（按顺序查找，越靠前优先级越高）。

    环境变量支持两种写法：路径分隔符（Windows ``;`` / POSIX ``:``，与 ``PATH``
    同口径）或 JSON 数组。**不用逗号**——路径本身可能含逗号，按逗号切会把
    一个目录拆成两个不存在的目录，而失败表现为「技能没加载」这种难排查的
    现象。
    """

    # ---------------- 会话 ----------------
    thread_title_max_chars: int = Field(default=24, ge=1, le=200)
    """**自动生成**标题的字符上限（按首条用户输入生成，超出以省略号截断）。

    WHY 做成配置：不同前端宽度能承载的标题长度不同，硬编码会让窄侧栏溢出、
    宽侧栏浪费空间；而这里只约束「截断长度」，不参与任何存储结构。
    """

    thread_rename_max_chars: int = Field(default=120, ge=1, le=200)
    """**手动改名**允许的字符上限。

    WHY 与 ``thread_title_max_chars`` 分开：后者是自动标题的生成口径（很短，
    只为列表可读），而用户手写的标题常常带上下文（「排查登录超时 - 2026Q3」），
    用 24 字符去卡手工输入等于逼用户起一个没信息量的名字。
    WHY 上限不超过 200：存储层的标题硬上限是 200，服务层必须比它更严，
    否则用户输入的标题会在入库时被静默截断——那比直接报错更让人困惑。
    """

    tool_result_preview_chars: int = Field(default=2000, ge=100, le=50_000)
    """推送给前端的工具结果预览长度上限。

    WHY 做成配置：命令输出或大文件读取可达数十万字符，直接推送会占满带宽并
    让界面卡死；而不同部署的前端能承载的预览长度不同，硬编码无法按环境调整。
    """

    # ---------------- 执行与安全 ----------------
    execution_mode: ExecutionMode = ExecutionMode.DISABLED
    shell_timeout: int = Field(default=120, gt=0)
    shell_max_output_bytes: int = Field(default=100_000, gt=0)

    # ---------------- 沙箱（仅 sandbox 档位生效） ----------------
    sandbox_tier: SandboxTier = SandboxTier.AUTO
    """隔离档位；``auto`` 会落到当前已实现的最高档位。"""

    sandbox_timeout: int = Field(default=120, gt=0)
    """单条命令的超时秒数。

    WHY 独立于 ``shell_timeout``：``shell_timeout`` 是 ``local`` 档位的口径，
    沙箱档位需要独立的资源与超时策略，二者混用会导致调一个影响另一个。
    """

    sandbox_max_output_bytes: int = Field(default=100_000, gt=0)
    """stdout / stderr 各自的截断阈值。"""

    sandbox_max_processes: int = Field(default=64, ge=1)
    """活动进程数上限。

    WHY 必须有：LLM 生成或复制来的命令里出现 fork bomb 的概率不高，但
    一旦出现，宿主机在几秒内失去响应，且只能靠重启恢复——上限是唯一防线。
    """

    sandbox_max_memory_mb: int = Field(default=2048, ge=64)
    """单个进程的内存上限（MB）。"""

    sandbox_cpu_percent: int = Field(default=50, ge=1, le=100)
    """CPU 占用硬上限百分比（Windows Job Object 生效）。"""

    sandbox_env_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ENV_ALLOWLIST)
    )
    """允许传入子进程的环境变量白名单。

    WHY 白名单：宿主机环境常含 ``*_API_KEY``、云凭证、``USERPROFILE``，
    黑名单补不全；命令真正需要的变量只有固定的少数几个。

    环境变量支持两种写法：逗号分隔（``SANDBOX_ENV_ALLOWLIST=PATH,TEMP``）
    或 JSON 数组。大小写无需在意——``SandboxPolicy.sanitized_env`` 两侧都按
    ``upper()`` 比较（Windows 环境变量名本身大小写不敏感）。
    """

    sandbox_network_mode: str = Field(default=NETWORK_MODE_NONE, pattern="^(none|host)$")
    """网络模式。

    ``none`` 仅表示不向子进程传递代理类变量；Windows 上进程级网络阻断需要
    管理员权限建防火墙规则，本期不做，残余风险由日志与文档显式标注。
    """

    sandbox_wsl_distro: str | None = None
    """WSL 档位使用的发行版名称；``None`` 时自动挑选首个满足要求的发行版。

    WHY 需要「显式指定」与「自动挑选」两条路：一台机器常有多个发行版，而
    默认发行版未必适合跑命令（例如 Docker Desktop 自带的精简发行版没有
    bash）；自动挑选会按 ``wsl --list --quiet`` 的顺序逐个探测，显式指定
    则能跳过这段冷启动开销。
    """

    sandbox_require_approval: bool = True
    """沙箱档位下是否仍需人工审批。

    WHY 默认开启：Tier 0 不是安全边界，防不住本地提权与凭据嗅探；审批是
    本档位真正的主防线，关闭它等于只剩资源管控。
    """

    # ---------------- 护栏 ----------------
    max_model_calls_per_run: int = Field(default=60, gt=0)
    recursion_limit: int = Field(default=100, gt=0)

    # ---------------- 运行治理 ----------------
    run_max_seconds: int = Field(default=900, ge=0)
    """单轮运行的最长秒数；超时由后台巡检协程强制取消。

    WHY 必须有：模型调用卡在网络层、工具死循环、用户忘记点停止，都会让一轮
    运行无限期占用槽位——该会话此后无法再发起任何对话，只能重启进程。

    WHY 允许 ``0``：CLI 的一次性长任务与联调场景需要关闭它，而「不限制」是
    一个显式选择，不应靠把阈值调到极大来变相实现。
    """

    hitl_pending_ttl_seconds: int = Field(default=1800, ge=0)
    """人工审批挂起的最长等待秒数；超期未决策的中断被标记过期。

    WHY 必须有：审批卡一旦无人处理就永久挂起——它既不占运行槽位（图已暂停），
    也不算错误，只有 TTL 能让「等待审批数」这个指标重新可信。

    WHY 允许 ``0``：与 ``run_max_seconds`` 同理，仅用于本地联调。
    """

    run_governance_interval_seconds: int = Field(default=30, ge=1)
    """运行治理协程的巡检间隔（秒）。

    WHY 与两个阈值分开配置：阈值决定「判什么为超时 / 过期」，间隔决定「多久
    扫一次」；间隔即超时判定的最大误差，短间隔更精确但更频繁地取运行快照。
    """

    # ---------------- 工具扩展（自定义工具 / MCP） ----------------
    custom_tool_modules: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """需要加载的自定义工具模块（点分路径）。

    模块需提供 ``TOOLS``（工具或可调用对象列表）或 ``register_tools(registry)``
    二者之一。WHY 走配置而不是在内核里 import：新增一个工具不应该修改内核
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

    # ---------------- HTTP 服务 ----------------
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"

    # ---------------- 认证与鉴权 ----------------
    auth_mode: Literal["disabled", "apikey", "oidc"] = "disabled"
    """认证模式。

    ``disabled``：保持原有行为，不校验身份（仅推荐本地开发）。
    ``apikey``：启用简单 API Key 认证，适合 CLI 与快速试用。
    ``oidc``：启用 OIDC RP 认证，对接 Authentik 等自托管 IdP。
    """

    auth_session_secret: str = Field(default="", repr=False)
    """本地会话 Cookie 签名密钥；auth_mode != disabled 时必须提供且不少于 32 字节。"""

    auth_cookie_name: str = "harness_session"
    auth_session_max_age_seconds: int = Field(default=28800, ge=60)
    auth_cookie_secure: bool = False
    """Cookie 的 Secure 标志；生产环境必须设为 ``True`` 并配合 HTTPS。"""
    auth_cookie_samesite: str = Field(default="lax", pattern="^(lax|strict|none)$")
    """Cookie 的 SameSite 属性；OIDC 回调需要浏览器带 Cookie，默认 ``lax``。"""

    # API Key 模式
    auth_api_key_header: str = "X-API-Key"
    auth_api_key_dev: str = Field(default="", repr=False)
    """开发用 API Key；生产环境应使用可轮换的 key store，禁止长期单 key。"""

    # OIDC Device Flow
    device_flow_expires_in_seconds: int = Field(default=600, ge=60)
    device_flow_poll_interval_seconds: int = Field(default=5, ge=1)
    device_flow_api_key_expires_in_days: int = Field(default=30, ge=1)
    oidc_device_flow_base_url: str = "http://127.0.0.1:8000"
    """CLI 在 OIDC 模式下做 Device Flow 时访问的 Web 服务地址。"""

    # 审计保留
    audit_retention_days: int = Field(default=180, ge=1)
    """审计日志保留天数；超期的记录会被定期清理任务删除。

    WHY 必须有保留策略：审计表随每次运行、审批、登录单调增长，长期运行的
    部署里它会成为最大的一张表，而超过保留期的记录在合规上通常已无留存
    必要。做成配置而非常量，是因为不同部署的合规要求差异极大。
    """

    audit_retention_interval_seconds: int = Field(default=86400, ge=60)
    """保留清理任务的执行间隔（秒）。

    WHY 与保留天数分开配置：保留期决定「删什么」，间隔决定「多久扫一次」；
    小部署希望每天清一次，大表则可能需要更频繁地分批清理。
    """

    audit_archive_enabled: bool = True
    """清理超期审计事件前是否先导出归档文件。

    WHY 默认开启：保留期一到就删，等于把「过期」和「可丢弃」划了等号——
    合规审计经常需要回溯保留期之前的记录。关闭后行为退化为「只删不导出」。

    WHY 归档失败要拦住删除（fail-closed）：目录不可写时若照删不误，数据就是
    静默丢失且无从补救；宁可让审计表继续增长并打出 ERROR 日志，也不能丢记录。
    """

    audit_archive_dir: Path = Field(default=Path("./.data/audit-archive"))
    """超期审计事件的归档目录。

    归档文件按批次写成 JSONL，运维可将其搬到对象存储后自行清理本目录。
    """

    audit_archive_batch_size: int = Field(default=500, ge=1, le=5000)
    """单次归档批次的条数。

    WHY 分批而不是一次读完：超期记录可能有几十万条，一次性载入内存会让
    后台清理任务把进程内存顶上去；分批读取 + 分批落盘使峰值内存与批次
    大小成正比，而与超期总量无关。
    """

    # ---------------- 用量统计 ----------------
    usage_default_window_days: int = Field(default=7, ge=1, le=3650)
    """/api/usage 的默认统计窗口天数。

    WHY 做成配置：不同团队的结算周期不同（按天看成本、按月看预算），
    硬编码一个窗口会让多数组调用都要显式传参。
    """

    usage_max_window_days: int = Field(default=90, ge=1, le=3650)
    """/api/usage 允许查询的最大窗口天数。

    WHY 需要上限：窗口越大扫描的记录越多，无上限的接口可以被用来发起
    一次全表聚合，进而拖慢同一数据库上的会话读写。
    """

    # 认证端点限流
    auth_rate_limit_window_seconds: int = Field(default=60, ge=1)
    auth_rate_limit_max_attempts: int = Field(default=10, ge=1)
    """单 IP 在窗口内允许的最大认证请求数（login/callback/apikey 校验）。"""

    # OIDC 模式
    oidc_issuer: str = ""
    """IdP 的 issuer URL，例如 https://auth.example.com/application/o/myagentharness/。"""
    oidc_client_id: str = ""
    oidc_client_secret: str = Field(default="", repr=False)
    oidc_redirect_uri: str = ""
    oidc_scope: str = "openid profile email harness:threads:read harness:threads:write"

    @field_validator("auth_session_secret", mode="after")
    @classmethod
    def _validate_session_secret(cls, value: str, info: Any) -> str:
        """auth_mode != disabled 时必须提供足够长的会话密钥。"""
        mode = info.data.get("auth_mode")
        if mode and mode != "disabled" and len(value) < 32:
            raise ValueError("auth_mode 非 disabled 时，auth_session_secret 至少需要 32 字节")
        return value

    @field_validator("oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_redirect_uri", mode="after")
    @classmethod
    def _validate_oidc_fields(cls, value: str, info: Any) -> str:
        """auth_mode == oidc 时 OIDC 相关字段不能为空。"""
        mode = info.data.get("auth_mode")
        if mode == "oidc" and not value:
            field_name = info.field_name or "OIDC 字段"
            raise ValueError(f"auth_mode=oidc 时，{field_name} 不能为空")
        return value

    @field_validator("workspace", "memory_file", "db_path", mode="after")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        """展开用户目录并转绝对路径。

        WHY 统一在此处理：backend 的 ``root_dir`` 若用相对路径，进程工作目录
        一旦变化就会指向不同的物理目录，属于难以复现的隐患。
        """
        return value.expanduser().resolve()

    @field_validator("skill_dirs", mode="before")
    @classmethod
    def _parse_skill_dirs(cls, value: object) -> object:
        """按路径分隔符切分技能目录，口径见 ``parse_list_config``。

        WHY 用 ``os.pathsep`` 而不是逗号：路径本身可能含逗号，按逗号切会把
        一个目录拆成两个不存在的目录，而症状是「技能没加载」——排障时不会
        有人想到去查分隔符。
        """
        return parse_list_config(value, field="skill_dirs", separators=(os.pathsep,))

    @field_validator("sandbox_env_allowlist", mode="before")
    @classmethod
    def _parse_env_allowlist(cls, value: object) -> object:
        """按逗号切分环境变量白名单，口径见 ``parse_list_config``。"""
        return parse_list_config(value, field="sandbox_env_allowlist", separators=(",",))

    @field_validator("skill_dirs", mode="after")
    @classmethod
    def _expand_dirs(cls, value: list[Path]) -> list[Path]:
        return [item.expanduser().resolve() for item in value]

    @field_validator("execution_mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: object) -> object:
        """容错大小写与空白，避免 ``LOCAL`` / `` local `` 被当成非法值。"""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("sandbox_tier", mode="before")
    @classmethod
    def _normalize_tier(cls, value: object) -> object:
        """容错大小写与空白，与 ``execution_mode`` 保持同一口径。"""
        if isinstance(value, str):
            return value.strip().lower()
        return value

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

    @field_validator("sandbox_wsl_distro", mode="before")
    @classmethod
    def _blank_distro_to_none(cls, value: object) -> object:
        """把空串归一为 ``None``。

        WHY：``SANDBOX_WSL_DISTRO=""`` 是 shell 里「清空变量」的常见写法，
        若当成一个发行版名去探测，用户只会看到一条与真实意图无关的报错。
        """
        if isinstance(value, str):
            return value.strip() or None
        return value

    def warn_if_unauthenticated_exposure(self, effective_host: str | None = None) -> bool:
        """绑定非回环地址且未启用认证时，输出 ERROR 级告警。

        WHY 告警而不是拒绝启动：本项目默认绑定 ``127.0.0.1``，硬拒绝会让
        「改绑内网地址做联调」这类正当场景被误伤；要消除的只是「静默」——
        一旦服务对非本机可达且没有任何认证，任何能访问该地址的客户端都能
        直接使用 ``execute`` 等工具，操作者必须有机会看见这件事。

        WHY 接受 ``effective_host`` 覆盖：``python main.py web --host`` 的
        命令行参数优先于配置，若只读 ``self.host``，加一个 ``--host 0.0.0.0``
        就能绕过本检查——检查必须盯住「最终真正绑定的地址」。

        Args:
            effective_host: 实际生效的监听地址；``None`` 表示取 ``self.host``。

        Returns:
            True 表示已发出告警（非回环 + 未启用认证）；False 表示无需告警
            （绑定回环，或已启用认证）。

        Raises:
            ValueError: ``effective_host`` 既非 ``None`` 也非字符串。
        """
        if effective_host is not None and not isinstance(effective_host, str):
            msg = f"effective_host 必须是字符串或 None，实际：{type(effective_host).__name__}"
            logger.error("%s", msg)
            raise ValueError(msg)

        if self.auth_mode != "disabled":
            # 已启用认证就不属于「未认证暴露」；此处若也告警，告警会失去指向性
            return False

        bind_host = self.host if effective_host is None else effective_host
        if is_loopback_host(bind_host):
            return False

        logger.error(
            "未认证暴露风险：监听地址 %r 不是本机回环，且 AUTH_MODE=disabled——"
            "任何能访问该地址的客户端都可直接使用本服务（含 execute 工具）；"
            "请改绑 127.0.0.1，或设置 AUTH_MODE=apikey|oidc",
            bind_host,
        )
        return True

    def ensure_directories(self) -> None:
        """创建运行时必需的目录并记录真实落点。

        WHY 显式创建：``FilesystemBackend`` 与 SQLite 都需要父目录存在，
        缺少时报错信息通常与根因无关，排查成本高。
        """
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.memory_file.parent != Path("."):
            self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        for directory in self.skill_dirs:
            directory.mkdir(parents=True, exist_ok=True)

    def existing_skill_dirs(self) -> list[Path]:
        """返回真实存在的技能目录。

        WHY 过滤而非报错：技能库是可选能力，缺失只应降级而不是让应用无法启动。
        """
        existing = [item for item in self.skill_dirs if item.is_dir()]
        missing = [item for item in self.skill_dirs if item not in existing]
        if missing:
            logger.warning("以下技能目录不存在，已跳过：%s", [str(item) for item in missing])
        return existing

    def active_mcp_servers(self) -> list[MCPServerSpec]:
        """返回本次启动需要真正连接的 MCP 服务器。

        WHY 单独提供该方法而不是让调用方自己判 ``mcp_enabled``：开关语义
        （总开关 + 单 server 开关）只有一份定义，散到调用方后必然出现
        「总开关关了但仍尝试建连」这类不一致。
        """
        if not self.mcp_enabled:
            if self.mcp_servers:
                logger.info("MCP 总开关已关闭，跳过 %d 个已配置的服务器", len(self.mcp_servers))
            return []
        return [spec for spec in self.mcp_servers if spec.enabled]

    def skill_source_paths(self) -> list[str]:
        """返回 deepagents ``skills`` 参数所需的 POSIX 路径列表。

        WHY 必须换算成相对工作区的虚拟路径：技能由 ``SkillsMiddleware`` 经
        backend 读取，而 backend 的根就是 ``workspace``，直接传宿主机绝对路径
        在虚拟模式下会被当成工作区内的子路径，导致技能永远命中不了。

        位于工作区之外的目录无法映射，只能跳过并记录告警。
        """
        resolved: list[str] = []
        for directory in self.existing_skill_dirs():
            try:
                relative = directory.relative_to(self.workspace)
            except ValueError:
                logger.warning(
                    "技能目录不在工作区内，已跳过：%s（工作区：%s）",
                    directory,
                    self.workspace,
                )
                continue
            resolved.append("/" + relative.as_posix())
        return resolved

    @property
    def memory_paths(self) -> list[str]:
        """返回 deepagents ``memory`` 参数所需的 POSIX 路径列表。

        WHY 必须是 POSIX 正斜杠且相对工作区：虚拟文件系统内部以 ``/`` 作为根
        分隔符，Windows 原生分隔符会被当成普通字符；同时 backend 的根就是
        ``workspace``，绝对路径同样解析不到。
        """
        if not self.memory_file.is_file():
            logger.warning("长期记忆文件不存在，跳过加载：%s", self.memory_file)
            return []

        try:
            relative = self.memory_file.relative_to(self.workspace)
        except ValueError:
            logger.warning(
                "长期记忆文件不在工作区内，已跳过：%s（工作区：%s）",
                self.memory_file,
                self.workspace,
            )
            return []
        return ["/" + relative.as_posix()]

    @classmethod
    def load(cls) -> AppConfig:
        """加载并完成一次性的落地校验与目录准备。"""
        instance = cls()
        instance.ensure_directories()
        logger.info(
            "配置加载完成：model=%s mode=%s tier=%s workspace=%s",
            instance.default_model,
            instance.execution_mode,
            instance.sandbox_tier,
            instance.workspace,
        )
        return instance


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """进程内共享同一份配置。

    WHY 缓存：Web 服务每个请求都会用到配置，重复解析 ``.env`` 既浪费 IO，
    也可能导致同一进程内出现两份不一致的路径。
    """
    return AppConfig.load()
