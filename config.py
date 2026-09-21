"""统一配置中心。

所有环境变量、路径与运行时参数只在此处声明与校验，避免散落到各个模块后
出现多处重复解析、校验口径不一致的问题。

读取优先级：环境变量 > .env 文件 > 字段默认值。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
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

DEFAULT_ATTACHMENT_MIME_TYPES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
)
"""默认允许上传的附件类型。

WHY 只放图片：这一项的实质约束是「模型能不能收下这种内容」。四种图片类型是各
provider 的多模态接口共同支持的集合；把 PDF / 文本也放进来会得到一个「上传成功、
但构造消息时才失败」的入口，而那正是本任务要消除的静默失败。
"""

_APP_ROOT = Path(__file__).resolve().parent
"""应用自身的目录（源码树根，容器内为 ``/app``）。

WHY 用 ``__file__`` 定位而不是 ``./``：按进程工作目录解析的默认值会在「从别的目录
启动进程」时指向一个不存在的路径，而表现是内置资产凭空消失，没有任何报错。
"""

BUILTIN_SKILLS_DIR: Path = _APP_ROOT / "skills-builtin"
"""随应用交付的内置技能目录。

WHY 不在用户工作区内：工作区由用户显式指定（可能是任意项目目录），把随产品交付的
资产写进别人的项目里既越界，也会在用户换一个工作区之后失效——而失效形态只是几行
WARNING，功能上是「内置技能装了却用不上」。
"""

_MEMORY_FILE_NAME = "AGENTS.md"
"""长期记忆文件的默认文件名（与 ``AGENTS.md`` 生态惯例一致）。"""

GLOBAL_MEMORY_PREFIX = "/global/"
"""**全局**长期记忆在虚拟文件系统里的挂载前缀。

WHY 需要独立前缀、不直接挂在虚拟根上：虚拟根就是**本会话的工作区**，``/AGENTS.md``
在那里表示「这个工作区自带的说明文件」。两者来源不同（一个跨全部会话、一个只对落在该
工作区的会话），挤到同一个路径上就只能二选一——而用户要的是「全局那份额外对我生效」。
"""

_ROOTS_STORE_DIR_NAME = "roots"
"""根外存储的父目录名（在数据目录下）：一个会话根一个子目录。

WHY 跟着数据目录、不做成配置项：它必须与数据目录**同卷**（搬迁/备份只搬数据目录这件事
才成立），而多一个配置项只会多一种「配到别的盘」的机会，换不来任何能力。
"""

_SKILLS_STORE_DIR_NAME = "skills"
"""技能库子目录名（位于某个根的存储目录里）：用户放技能包的地方。"""

_SKILL_VIEW_STORE_DIR_NAME = "skills-active"
"""技能视图子目录名（同上）：只把**启用中**的技能物化出来的派生物。"""

_TOOL_OUTPUTS_STORE_DIR_NAME = "tool-outputs"
"""工具输出留存子目录名（同上）。"""

VIRTUAL_SKILLS = "/skills"
"""技能库在虚拟文件系统里的挂载点（Agent 只读）。"""

VIRTUAL_BUILTIN_SKILLS = "/skills-builtin"
"""随应用交付的内置技能在虚拟文件系统里的挂载点（Agent 只读）。"""

VIRTUAL_SKILL_VIEW = "/.skills-active"
"""技能视图在虚拟文件系统里的挂载点：**建图时的技能来源就是它**。

WHY 名字带点：它是程序产物，不是用户的资料——虽然它已经不在工作区里（面板不会再遍历到
它），但保留这个前缀能让日志与虚拟路径一眼区分「技能包本体」与「它的派生物」。
"""

VIRTUAL_TOOL_OUTPUTS = "/_tool_outputs"
"""工具输出留存在虚拟文件系统里的挂载点。

WHY 名字带下划线：与技能视图同理——它是系统的旁路留存，不是任务成果；消息与日志里出现
``/_tool_outputs/...`` 时，读者应当立刻知道那是「可回取的完整输出」而不是用户文件。
"""

_USER_SKILLS_DIR_NAME = "skills"
"""旧版本里「会话根下用户技能目录」的目录名。

WHY 还留着它：根外存储落地后，技能库搬到了 ``<数据目录>/roots/<根标识>/skills``，但
**工作区里可能还留着旧位置**——启动时要能认出它并把技能包搬过去（见
``SessionRoot._migrate_legacy_skills``）。名字另取一个常量会让迁移代码与旧布局对不上。
"""

_SESSIONS_DIR_NAME = "sessions"
"""未绑定工作空间的会话，其专属目录在数据目录下的默认位置。"""

_UNSAFE_DIR_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _readable_dir_name(root: Path, *, limit: int = 32) -> str:
    """把工作区路径的最后一段压成一个可安全用作目录名的可读片段。

    WHY 要可读：``roots/`` 下会住着十几个存储目录，纯哈希名在排障时无法对应回工作区。
    WHY 还要截断与清洗：目录名可能含空格、中文、超长路径段，直接拼进路径既可能超出平台
    上限，也可能与分隔符撞上。

    Args:
        root: 工作区根目录。
        limit: 片段的最大字符数。

    Returns:
        仅含 ``[A-Za-z0-9._-]`` 的片段；清洗后为空时返回 ``root``。
    """
    cleaned = _UNSAFE_DIR_NAME.sub("_", root.name.strip())[:limit].strip("._")
    return cleaned or "root"


@dataclass(frozen=True)
class SkillSource:
    """一个技能来源：宿主机上的真实目录 + 它在虚拟文件系统里的挂载路径。

    WHY 需要这一对：技能包由 ``SkillsMiddleware`` 经 backend 读取，而 backend 只认虚拟
    路径。技能库已经搬到工作区之外的存储目录（``SessionRoot.skills_store``），内置技能
    随应用交付、同样在工作区之外——两者都必须显式挂一个虚拟路径才读得到
    （见 ``SessionRoot.read_only_mounts``），否则**一个都读不到且没有任何告警**。
    """

    host_dir: Path
    virtual: str


class ExecutionMode(StrEnum):
    """``execute`` 工具的执行档位。

    local:    宿主机直跑 shell，仅限本机开发，与 ``LocalShellBackend`` 的安全
              警告一致——绝不可用于 Web 或多租户环境。
    sandbox:  沙箱内执行 shell。隔离强度由 ``SandboxTier`` 决定，Tier 0 / 1 / 2
              （进程 / WSL / 容器）均已实现；都不是安全边界，须与人工审批配合。
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
    docker:  Tier 2——容器内执行。**这是第一个真正限制命令可见面的档位**：
             默认只见镜像内容与显式挂载的工作区，宿主其余路径在容器内不存在；
             网络默认切断（实测 ``--network none`` 下无法解析域名），进程树随
             容器消失。仍**不宣称强隔离**——容器共享宿主内核，内核漏洞逃逸与
             侧信道不在防护范围内。必须与人工审批配合（见 ``agent/guardrails.py``）。
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


class EmbeddingBackendKind(StrEnum):
    """知识库的嵌入后端档位。

    none:          不装配后端。知识库仍可用，但退化为关键词检索——**这是一等
                   档位，不是故障**，因此检索路径要按「有没有嵌入」分支，而不是
                   把「没有」当成异常。
    openai-compat: 调用任意兼容 ``POST /v1/embeddings`` 的服务（OpenAI、TEI、
                   Ollama、各家云厂商）。**容器内加载模型走这一档**——应用不做
                   容器编排（那属 T24），只管往一个地址发请求。
    subprocess:    本机自托管。在独立 venv 内拉起瘦服务进程，按 stdio 通信；
                   主进程不引入 onnxruntime，模型可在空闲时整个回收。
    """

    NONE = "none"
    OPENAI_COMPAT = "openai-compat"
    SUBPROCESS = "subprocess"


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

    vision_model_aliases: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["openai", "anthropic"]
    )
    """哪些**模型别名**支持图片输入（多模态）。

    WHY 做成配置而不是写死在 ``ModelSpec`` 里：能力取决于**实际模型**，而模型名
    是可配置的（``OPENAI_MODEL`` 可以被指向一个纯文本模型）。写死会在「照着文档
    换了个模型」之后继续允许上传，直到构造消息时才失败——那正是本任务要消除的
    静默失败。默认值只覆盖各 provider 的官方默认模型。
    """



    # ---------------- 运行时路径 ----------------
    #
    # WHY 没有「默认工作空间」这一项：会话的工作空间由用户在**创建会话时**指定，也可以
    # 不指定。配置里再放一个默认值，就等于把「不指定」偷偷变成「指定了配置里那个」——
    # 而这两种选择在本模型下必须产生不同的结果（后者落到应用管理的会话专属目录里）。
    #
    memory_file: Path | None = None
    """**全局**长期记忆文件（人工维护，Agent 只读）。

    ``None`` 表示不配全局记忆，改用 ``<会话根>/AGENTS.md``（若存在）——那一份是**工作区
    自带**的说明文件，只对落在该工作区的会话生效。

    WHY 显式配置的这一份按「全局」对待：它的用途就是跨会话共享的偏好与约定（称呼、
    语言、项目惯例）。要求它落在会话根之内，等于要求用户为每条会话各维护一份，而那恰好
    是它要消除的重复。它由应用以**只读挂载**的方式进入虚拟文件系统
    （``/global/<文件名>``，见 ``SessionRoot.memory_plan``），因此不需要、也不应该位于
    任何工作区内部。
    """

    db_path: Path = Field(default=Path("./.data/agent.db"))

    sessions_root: Path | None = None
    """**未绑定工作空间的会话**其专属目录的父目录。

    ``None`` 表示按 ``<数据目录>/sessions`` 派生（见 ``resolved_sessions_root``）——
    数据目录就是 ``DB_PATH`` 所在目录。WHY 跟着数据目录走：会话的检查点、审计与用量
    都在那个库里，把「会话专属目录」放在别处会让「备份/搬迁只需要搬一个目录」不再成立。

    WHY 需要它：要求「不绑定工作空间的会话，在应用指定的文件夹下自动创建独立子文件夹」。
    没有这一项，那些会话就没有文件根——而 Agent 的文件工具、沙箱挂载根、附件与知识库
    索引都需要一个根。

    每个会话的子目录名就是它的会话 ID（见 ``session_dir``）。
    """

    skill_dirs: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    """技能目录（按顺序查找）。**空列表表示按工作区派生**，见 ``SessionRoot``。

    **顺序有语义：越靠后优先级越高**（上游 ``SkillsMiddleware`` 的规则是同名技能由后面的
    来源覆盖前面的）。因此内置目录排在**前面**（低优先级），用户才能用同名技能覆盖内置的
    那一个；顺序反过来会让内置技能永远赢，而「我改了却不生效」不会有任何报错。

    为空时每个根派生两项：随应用交付的 ``BUILTIN_SKILLS_DIR``（应用目录里），以及该根的
    技能库 ``SessionRoot.skills_store``（在**根外存储**里，不在工作区内）。两项都在工作区
    之外，因此都由 ``read_only_mounts`` 挂上虚拟路径——backend 只认虚拟路径，不挂载就会
    「一个都读不到且没有任何告警」。

    WHY 一旦显式配置就**对所有工作区生效**（不再按工作区分叉）：显式给的是绝对路径，
    它表达的是「技能包放在这些固定位置」，与当前是哪个工作区无关。

    环境变量支持两种写法：路径分隔符（Windows ``;`` / POSIX ``:``，与 ``PATH``
    同口径）或 JSON 数组。**不用逗号**——路径本身可能含逗号，按逗号切会把
    一个目录拆成两个不存在的目录，而失败表现为「技能没加载」这种难排查的
    现象。
    """

    # ---------------- 工作区文件（Web 文件面板） ----------------
    workspace_list_max_entries: int = Field(default=500, ge=1, le=5000)
    """单次列目录返回的最大条目数。

    WHY 需要上限：工作区里常有 ``node_modules`` 这类上万条的目录，无上限地返回
    会让一次展开变成一次大数据传输，也会让前端渲染卡死。
    """

    workspace_file_preview_chars: int = Field(default=20_000, ge=100, le=500_000)
    """文件面板里文本预览的字符上限（超出截断并在响应里标注）。"""

    workspace_file_max_bytes: int = Field(default=5_000_000, ge=1024)
    """超过此字节数的文件不做文本预览，只回 ``too_large`` 降级标记。

    WHY 用降级标记而不是报错：文件确实存在、也确实读得到，只是不适合整份塞进
    浏览器。当成错误会让界面只能显示一句失败，而用户真正想知道的是「它有多大」。
    """

    # ---------------- 附件（上传与多模态） ----------------
    attachment_max_bytes: int = Field(default=2_000_000, ge=1024, le=50_000_000)
    """单个附件的字节上限。

    WHY 默认只有 2 MB（远小于 ``workspace_file_max_bytes``）：附件的内容会以
    data URL 形式进入**用户消息**，而消息要被写进检查点并在后续每一轮里重新发给
    模型。上限放宽一倍，检查点与每次请求的体积就跟着翻一倍——这个开销是持续的，
    不是一次性的。
    """

    attachment_max_per_thread: int = Field(default=8, ge=1, le=100)
    """单个会话允许保留的附件数上限。

    WHY 需要它：附件目录在工作区里只增不减（会话存续期间），而没有上限时一次
    误操作就能把工作区塞满；上限也顺带把「一次请求塞多少图片给模型」框住了。
    """

    attachment_allowed_mime_types: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ATTACHMENT_MIME_TYPES)
    )
    """允许上传的 MIME 白名单。

    WHY 白名单而不是黑名单：MIME 是调用方自己声明的，黑名单永远补不全；
    而这里真正的约束是「模型能不能收下这种内容」，只有少数几种图片类型成立。
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

    tool_output_max_chars: int = Field(default=200_000, ge=1000)
    """单个留存文件的字符上限（被截断的工具输出会完整落盘到工作区）。

    WHY 仍要上限：留存是为「能回取」，不是做无限仓库；一次读到几十 MB 文件的调用
    若原样落盘，磁盘会随对话量无界增长。截断副本配上首行说明已足以回答
    「这次调用产出了什么」。
    """

    tool_output_retention_per_thread: int = Field(default=20, ge=1, le=1000)
    """每个会话保留的最新工具输出份数，超出后按时间清理旧文件。

    WHY 必须清理：留存目录只增不减会变成磁盘黑洞，而真正有用的只有最近若干次
    ——旧输出对应的是已经翻过去的对话。
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

    sandbox_docker_image: str = "harness-sandbox:latest"
    """``docker`` 档位使用的执行镜像名。

    由 ``scripts/setup_sandbox_image.py`` 依据 ``docker/sandbox.Dockerfile`` 构建。
    WHY 默认指向自建镜像而不是 ``python:3.14-slim``：自建那份以非 root 身份运行、
    不预装任何额外工具；直接指向官方镜像会把「用哪个镜像」这件事的默认值交给一次
    依赖联网的拉取。
    """

    sandbox_docker_workspace_read_only: bool = Field(default=False)
    """是否把工作区以只读方式挂载进容器。

    WHY 默认读写：``execute`` 的主要用途就是跑脚本与构建，产物就落在工作区里，
    只读会让这个档位不可用。需要更严边界的部署可以打开它——那时容器内的命令改不了
    工作区，而 Agent 自己的文件工具（走宿主侧）不受影响。
    """

    sandbox_docker_user: str = ""
    """传给 ``--user`` 的值（形如 ``1000:1000``）；空串表示用镜像默认身份。

    WHY 需要它：容器内以 root 写出文件时，在 **Linux 宿主**上文件属主是 root，之后
    宿主进程可能改不动挂载目录；把这里设成宿主的 ``uid:gid`` 即可避免。Windows 宿主
    由 Docker Desktop 代为处理属主，留空即可（实测容器内写入回到宿主可正常读写）。
    """

    sandbox_require_approval: bool = True
    """沙箱档位下是否仍需人工审批。

    WHY 默认开启：Tier 0 不是安全边界，防不住本地提权与凭据嗅探；审批是
    本档位真正的主防线，关闭它等于只剩资源管控。
    WHY 在 ``docker`` 档位上**同样**默认开启：隔离缩小的是「命令碰到了会怎样」，
    不是「这条命令该不该跑」——一条 ``rm -rf /work`` 在容器里照样能删掉挂载进来的
    工作区。审批与隔离回答的是两个不同问题，不可互相替代（见 T24 的定稿记录）。
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

    # ---------------- 联网工具（内置检索与抓取） ----------------
    web_search_provider: Literal["none", "tavily", "searxng"] = "none"
    """检索 provider。

    WHY 默认 ``none`` 而不是某个真实 provider：联网检索会把用户的查询词
    发给第三方，默认打开等于替用户做了这个决定。

    ``tavily``：托管检索服务，需要密钥；``searxng``：自建元搜索，需要地址、
    不需要密钥（与 ``ollama`` 同属「显式提供地址即可用」）。
    """

    web_search_api_key: str = Field(default="", repr=False)
    """检索服务密钥；``searxng`` 不需要。"""

    web_search_base_url: str = ""
    """检索服务地址；留空时用 provider 的官方地址（``searxng`` 必须显式提供）。"""

    web_search_timeout_seconds: float = Field(default=15.0, gt=0)
    """单次检索请求的超时秒数。"""

    web_search_max_results: int = Field(default=5, ge=1, le=20)
    """检索返回的结果条数上限。

    WHY 必须有上限：检索结果会整体进入上下文，条数不设限时一次检索就可能
    挤掉对话历史；上限也直接决定上游计费量。
    """

    web_fetch_timeout_seconds: float = Field(default=20.0, gt=0)
    """单次网页抓取请求的超时秒数。"""

    web_fetch_max_chars: int = Field(default=20_000, ge=500, le=500_000)
    """抓取正文的字符上限（超出部分截断并在输出中显式标注）。"""

    web_fetch_max_redirects: int = Field(default=3, ge=0, le=10)
    """允许跟随的 HTTP 重定向上限。

    WHY 必须限制：每一跳都是一次新的出站请求，而下一跳的地址由**上一次响应
    的 Location 头**决定——不设上限就等于把「还能访问哪些地址」的控制权交给
    远端，而逐跳校验正是 SSRF 防线中最容易被绕过的一环。
    """

    web_user_agent: str = ""
    """出站请求的 User-Agent；留空时用内置默认值。"""

    # ---------------- 嵌入后端（知识库的语义能力） ----------------
    embedding_backend: EmbeddingBackendKind = EmbeddingBackendKind.NONE
    """嵌入后端档位；各档位取舍见 ``EmbeddingBackendKind``。

    WHY 默认 ``none``：与 ``web_search_provider`` 同一口径——语义嵌入要么把文档内容
    发给第三方服务，要么在本机常驻一个实测 189 MB 的模型进程，两者都是「替用户做的
    决定」，不应跟着默认值开启。
    """

    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    """嵌入模型名。

    ``openai-compat`` 档位下它作为请求体的 ``model`` 字段原样发出（服务端据此定位
    要用的模型）；``subprocess`` 档位下它是 fastembed 的模型标识。
    """

    embedding_base_url: str = ""
    """``openai-compat`` 档位的服务地址（不含 ``/v1``，由实现拼接）。"""

    embedding_api_key: str = Field(default="", repr=False)
    """嵌入服务密钥；本地服务（TEI / Ollama）通常不需要。"""

    embedding_dims: int = Field(default=512, ge=1, le=8192)
    """向量维度。

    WHY 必须是**配置**而不是从首次响应里读回来：维度在建表时就要固定（向量表的列宽），
    而读回来的时机在插入之后——那时表已经建错了。它同时是「换了模型必须重建索引」的
    显式表达：换模型却不改这一项，插入会因维度不符而报错，而不是静默写进一批语义上
    无法互相比较的向量。
    """

    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    """单次嵌入请求的文本条数上限。"""

    embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    """单次嵌入往返（HTTP 请求或子进程一次问答）的超时秒数。

    WHY 比 ``llm_timeout`` 短：嵌入处在索引与检索的**同步阻塞**路径上，超时过长会让
    一次检索把整轮对话拖住。
    """

    embedding_idle_seconds: int = Field(default=600, ge=0)
    """``subprocess`` 档位下子进程的空闲回收秒数；``0`` 表示不回收。

    WHY 需要回收：知识库检索是偶发动作（一轮对话可能只在开头检索一次），而模型常驻
    内存实测 189 MB；不回收等于让一次偶发操作永久占住这份内存。
    """

    embedding_python: str = ""
    """``subprocess`` 档位使用的解释器路径。

    留空时用 ``.data/embed-venv`` 下的约定路径（由 ``scripts/setup_embed_venv.py``
    准备）。显式提供是为了让人能把模型装在自己选好的环境里，而不必迁就本项目的约定。
    """

    # ---------------- 知识库（工作区文档索引与检索） ----------------
    knowledge_chunk_chars: int = Field(default=800, ge=100, le=8000)
    """单个分块的目标字符数。

    WHY 以**字符**而不是 token 计量：切分发生在嵌入之前、模型之外，这一层拿不到
    分词器；而中英文的 token/字符比差异很大，用字符才能给出跨语言一致的行为。
    """

    knowledge_chunk_overlap_chars: int = Field(default=120, ge=0, le=2000)
    """相邻分块的重叠字符数。

    WHY 需要重叠：一句话被切断会同时毁掉两边的语义——左块丢了句尾、右块丢了句首，
    于是这句话在两个块里都检索不到。重叠让边界句在某一侧保持完整。
    """

    knowledge_search_top_k: int = Field(default=6, ge=1, le=50)
    """检索返回的块数上限。

    WHY 必须有上限：检索结果整体进入上下文，条数不设限时一次检索就能挤掉对话历史，
    也直接决定上游计费量。
    """

    knowledge_max_chunks_per_document: int = Field(default=500, ge=1, le=10000)
    """单个文档允许的分块数上限。

    WHY 必须有：分块数直接决定嵌入调用次数与向量表体积，而工作区里出现一份几 MB 的
    日志或生成文件是常事。超限时截断并记日志，而不是让一次索引把配额打满。
    """

    # ---------------- HTTP 服务 ----------------
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"

    # ---------------- 认证与鉴权 ----------------
    auth_mode: Literal["disabled", "apikey"] = "disabled"
    """认证模式。

    ``disabled``：不校验身份（仅推荐本地开发）。
    ``apikey``：API Key 认证，适合 CLI、脚本与集成方。
    """

    # API Key 模式
    auth_api_key_header: str = "X-API-Key"
    auth_api_key_dev: str = Field(default="", repr=False)
    """开发用 API Key；生产环境应使用可轮换的 key store，禁止长期单 key。"""

    harness_api_key: str = Field(default="", repr=False)
    """CLI 在 ``apikey`` 模式下出示的凭据（``python main.py cli``）。

    WHY 做成配置字段而不是让 CLI 自己读 ``os.environ``：``.env`` 的值只进
    ``AppConfig``、**不进**进程环境变量，CLI 若直接摸 ``os.environ``，
    「照着 .env.example 配好了却仍提示没配」就是必然结果。同名的真实环境变量
    照样生效——pydantic-settings 中环境变量的优先级高于 ``.env``，因此容器里
    ``docker compose exec -e HARNESS_API_KEY=...`` 的写法不受影响。
    """

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

    # 运行并发与限流
    max_concurrent_runs: int = Field(default=4, ge=0)
    """全进程允许同时进行的大模型运行数；``0`` 表示不限制。

    WHY 默认给一个具体值而不是「不限制」：不限并发时，几条长任务就能同时吃掉上游
    配额与本地内存，而那种过载在指标上只表现为「运行数很高」——看着正忙，其实已经
    排不动了。单用户场景下 4 远高于实际并发，等于没有影响。
    """

    run_rate_limit_window_seconds: int = Field(default=60, ge=1)
    run_rate_limit_max_attempts: int = Field(default=30, ge=1)
    """单个主体在窗口内允许发起的运行数。

    WHY 键取 owner_id 而不是 IP：一个 NAT 出口后面可能坐着一整个团队，按 IP 计数
    会把同事的正常使用算成一个人的滥用；而限流要挡的是「某个账号在刷」。
    """

    run_rejected_retry_after_seconds: int = Field(default=5, ge=1)
    """被限流或超出并发上限时回给客户端的 Retry-After 秒数。"""

    # 日志形态
    log_format: Literal["json", "text"] = "text"
    """日志输出格式；``text`` 供本机阅读，``json`` 供采集系统解析。

    WHY 默认 ``text``：本机开发时肉眼读日志是最主要的用法，默认切成 JSON 会让
    每一次本地排障都先过一道格式转换。结构化是「上线时需要」的能力，不是默认形态。
    """

    @field_validator("memory_file", "db_path", "sessions_root", mode="after")
    @classmethod
    def _expand_path(cls, value: Path | None) -> Path | None:
        """展开用户目录并转绝对路径；``None`` 表示「未配置，稍后派生」。

        WHY 统一在这里转绝对路径：backend 的 ``root_dir`` 与会话目录若用相对路径，
        进程工作目录一旦变化就会指向不同的物理目录，属于难以复现的隐患。
        """
        if value is None:
            return None
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

    @field_validator("attachment_allowed_mime_types", "vision_model_aliases", mode="before")
    @classmethod
    def _parse_csv_lists(cls, value: object) -> object:
        """按逗号切分这两个列表项，口径见 ``parse_list_config``。

        WHY 与 ``sandbox_env_allowlist`` 分开注册：分隔符相同但语义不同，合并成
        一个 validator 会让日后其中一个改口径时把另一个一起改掉。
        """
        return parse_list_config(value, field="attachment_allowed_mime_types", separators=(",",))

    @field_validator("attachment_allowed_mime_types", mode="after")
    @classmethod
    def _normalize_mime_types(cls, value: list[str]) -> list[str]:
        """归一 MIME 写法（去空白、转小写、去重保序）。

        WHY 必须归一：白名单要与上传请求声明的 MIME 做**精确比较**，而
        ``Image/PNG`` 与 ``image/png`` 在字符串层面不同、在语义上相同——不归一
        就会表现为「明明配了却拒绝上传」。
        """
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            candidate = str(item).split(";", 1)[0].strip().lower()
            if not candidate:
                continue
            if not re.match(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$", candidate):
                raise ValueError(f"attachment_allowed_mime_types 含非法 MIME：{item!r}")
            if candidate not in seen:
                seen.add(candidate)
                normalized.append(candidate)
        if not normalized:
            raise ValueError("attachment_allowed_mime_types 不能为空，否则任何附件都无法上传")
        return normalized

    @field_validator("vision_model_aliases", mode="after")
    @classmethod
    def _normalize_aliases(cls, value: list[str]) -> list[str]:
        """归一模型别名（去空白、去重保序）；允许为空（表示没有任何多模态模型）。"""
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            candidate = str(item).strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                normalized.append(candidate)
        return normalized

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

    @model_validator(mode="after")
    def _validate_knowledge_chunking(self) -> AppConfig:
        """校验分块参数的自洽性。

        WHY 在加载期拦下：重叠大于等于块长时，切分会在同一处反复推进而无法前进
        ——表现为索引卡死或产出满天飞的重复块，而原因要到切分器内部才看得出来。

        Raises:
            ValueError: 重叠字符数不小于块长。
        """
        if self.knowledge_chunk_overlap_chars >= self.knowledge_chunk_chars:
            raise ValueError(
                f"KNOWLEDGE_CHUNK_OVERLAP_CHARS（{self.knowledge_chunk_overlap_chars}）"
                f"必须小于 KNOWLEDGE_CHUNK_CHARS（{self.knowledge_chunk_chars}），"
                "否则切分会原地打转"
            )
        return self

    @field_validator("embedding_backend", mode="before")
    @classmethod
    def _normalize_embedding_backend(cls, value: object) -> object:
        """容错大小写与空白，与 ``execution_mode`` 保持同一口径。"""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @model_validator(mode="after")
    def _validate_embedding_backend(self) -> AppConfig:
        """按档位校验必需字段。

        WHY 在加载期拦而不是等首次嵌入：``openai-compat`` 缺地址时，异常会发生在
        一次索引或检索的深处，栈顶指向网络层；而配置自身的问题应当在启动时就能被
        指出来（与 ``MCPServerSpec`` 按传输方式校验同一个理由）。

        Raises:
            ValueError: ``openai-compat`` 档位未提供 ``embedding_base_url``。
        """
        if self.embedding_backend is EmbeddingBackendKind.OPENAI_COMPAT and not self.embedding_base_url.strip():
            raise ValueError(
                "EMBEDDING_BACKEND=openai-compat 必须提供 EMBEDDING_BASE_URL"
                "（例：容器内的嵌入服务 http://embed:80）"
            )
        return self

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
            "请改绑 127.0.0.1，或设置 AUTH_MODE=apikey",
            bind_host,
        )
        return True

    @property
    def resolved_sessions_root(self) -> Path:
        """未绑定工作空间的会话，其专属目录的父目录（绝对路径）。

        ``SESSIONS_ROOT`` 未配置时按 ``<数据目录>/sessions`` 派生——WHY 不写成一个固定
        默认值：数据目录是可以换的（``DB_PATH``），而「会话专属目录」与库在同一个
        可搬迁单元里，备份/搬迁才只需要搬一个目录。
        """
        if self.sessions_root is not None:
            return self.sessions_root
        return self.db_path.parent / _SESSIONS_DIR_NAME

    @property
    def roots_store_root(self) -> Path:
        """根外存储的父目录：``<数据目录>/roots``。

        WHY 派生而不是再加一项配置：它必须与数据目录**同卷**——否则「备份/搬迁只搬数据
        目录」这句话就不成立（技能库与工具留存会留在原地），而多一个配置项只会多一种
        「配到别的盘」的机会。会话专属目录已经按同一口径跟着数据目录走
        （``resolved_sessions_root``），这里保持一致。
        """
        return self.db_path.parent / _ROOTS_STORE_DIR_NAME

    def session_dir(self, thread_id: str) -> Path:
        """返回某个**未绑定工作空间**的会话的专属目录。

        WHY 用会话 ID 当子目录名：它就是这条会话的稳定标识，而「一个会话一个目录」正是
        不共享工作空间时要保证的事（两个会话落在同一个目录里会互相看到对方的产物）。

        WHY 必须校验 ID 是**单个安全的路径片段**：这个目录名会直接来自请求——前端在首次
        发送前申请一个 ID，服务端按它派生目录；面板、附件上传与首条消息的根解析都走同一条
        路径。而 ``Path(sessions_root) / "../../evil"`` 会解析到会话目录之外，于是 Agent 的
        文件根、附件与产物全部落在别处，且没有任何提示。拒绝而不是「清洗」：清洗会让两个
        不同的 ID 撞进同一个目录——那比报错危险得多。

        Args:
            thread_id: 会话 ID（已规范化）。

        Returns:
            ``<SESSIONS_ROOT>/<thread_id>``；目录本身按需创建。

        Raises:
            ValueError: ID 不是非空字符串、含路径分隔符或上级引用，或解析后落在会话目录
                之外（盘符、UNC 之类由最后一道兜住）。
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError(f"会话 ID 必须是非空字符串，实际：{thread_id!r}")
        separators = {"/", "\\", os.sep, os.altsep or os.sep}
        if thread_id in {".", ".."} or any(sep in thread_id for sep in separators):
            raise ValueError(f"会话 ID 不能包含路径分隔符或上级引用：{thread_id!r}")

        target = self.resolved_sessions_root / thread_id
        # WHY 还要再判一次包含关系：上面按字符判分隔符是**按平台**的（POSIX 上 ``a\b``
        # 是合法文件名，Windows 上却是两级路径），盘符、UNC 与前缀写法也各有各的坑。
        # 这一道只看结果：解析之后必须仍在会话目录之内。
        if not target.resolve().is_relative_to(self.resolved_sessions_root):
            raise ValueError(f"会话 ID 会解析到会话目录之外：{thread_id!r}")
        return target

    def ensure_directories(self) -> None:
        """创建启动所需的目录：数据目录、会话目录的父目录、根外存储的父目录、技能目录。

        WHY 显式创建：``FilesystemBackend`` 与 SQLite 都需要父目录存在，
        缺少时报错信息通常与根因无关，排查成本高。

        WHY 只建**父目录**、不建任何会话目录：会话专属目录要等到那条会话真的产生文件时
        才创建（见 ``SessionRoot.ensure_directories``）——启动时替所有会话建目录，会在
        用户还没发过消息时就在磁盘上留下一堆空目录。同理，``roots/`` 下也只在某个根真的
        被用到时才建它自己的存储目录（见 ``SessionRoot.ensure_storage``）。
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.resolved_sessions_root.mkdir(parents=True, exist_ok=True)
        self.roots_store_root.mkdir(parents=True, exist_ok=True)
        for directory in self.skill_dirs:
            directory.mkdir(parents=True, exist_ok=True)

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

    @classmethod
    def load(cls, **overrides: Any) -> AppConfig:
        """加载并完成一次性的落地校验与目录准备。

        Args:
            **overrides: 覆盖字段。**取值为 ``None`` 的项会被丢弃**：CLI 未提供参数时
                传进来的就是 ``None``，而它若被当作「显式设为 None」，会把 ``.env`` 里的
                值一起盖掉。

        Returns:
            已校验、已建目录的配置实例。

        Raises:
            ValidationError: 任一字段非法。
        """
        params = {key: value for key, value in overrides.items() if value is not None}
        instance = cls(**params)
        instance.ensure_directories()
        # WHY 在这里检查未知键：``extra="ignore"`` 让它们静默失效，而一句「配置里写着
        # 某个已不存在的项」足以把排查带到完全错误的方向（实测：残留的
        # ``WORKSPACE=./workspace`` 被读成「未绑定的会话会落到 ./workspace」）。
        instance.warn_unknown_env_keys(overrides.get("_env_file"))
        logger.info(
            "配置加载完成：model=%s mode=%s tier=%s sessions_root=%s",
            instance.default_model,
            instance.execution_mode,
            instance.sandbox_tier,
            instance.resolved_sessions_root,
        )
        return instance

    def warn_unknown_env_keys(self, env_file: Path | None = None) -> list[str]:
        """列出配置文件里**不被任何字段读取**的键并告警，返回这些键。

        WHY 需要它：``extra="ignore"`` 把未知键静默丢掉——这在部署里是必要的（同一份
        ``.env`` 常混着别的工具的变量，判成错误会让升级直接起不来），但**一声不响**
        这次实测出了代价：``.env`` 里残留的 ``WORKSPACE=./workspace``（旧模型的「默认
        工作空间」）读起来就是「未绑定的会话会落到 ./workspace」，而应用根本不读它。
        用户据此以为「自动建目录」没生效，排查方向被引到了完全错误的地方。

        WHY 只扫文件、不扫 ``os.environ``：进程环境里混着 shell / CI / 容器运行时的上百个
        变量，对它们逐个告警会把这条提示淹掉；而 ``.env`` 是本项目自己维护的那一份。

        Args:
            env_file: 要检查的文件；``None`` 表示取 ``model_config`` 里的 ``env_file``。

        Returns:
            未知键（去重、按出现顺序）；没有则返回空列表。
        """
        target = self._resolve_env_file(env_file)
        unknown = self._unknown_env_keys(target)
        if unknown:
            logger.warning(
                "配置文件里有 %d 个键不被任何字段读取，已忽略：%s（%s）——"
                "多半是旧版本的残留，删掉即可；留着它会让配置看起来在做一件实际没做的事",
                len(unknown),
                "、".join(unknown),
                target,
            )
        return unknown

    @classmethod
    def _resolve_env_file(cls, override: Any) -> Path | None:
        """取本次加载实际使用的 env 文件路径（``_env_file`` 覆盖优先）。

        Args:
            override: 调用方显式传入的 ``_env_file``；``None`` 表示用 ``model_config``
                里的默认值。

        Returns:
            文件路径；没有配置任何 env 文件时返回 ``None``。
        """
        raw = override if override is not None else cls.model_config.get("env_file")
        if raw is None:
            return None
        if isinstance(raw, (str, Path)):
            return Path(raw)
        if isinstance(raw, (list, tuple)) and raw:
            # pydantic-settings 允许给一串文件（后加载的覆盖先加载的）；这里看第一个足够了。
            return Path(str(raw[0]))
        return None

    @classmethod
    def _unknown_env_keys(cls, env_file: Path | None) -> list[str]:
        """扫出 ``env_file`` 里不被任何字段读取的键。

        WHY 按 ``KEY=VALUE`` 逐行解析而不是引入 dotenv：这里只做「键名是否被读过」的
        粗粒度判定，而多一个依赖换来的解析细节（引号、续行、插值）对这条提示没有影响。

        Args:
            env_file: 要检查的文件；``None`` 或文件不存在时返回空列表。

        Returns:
            未知键（去重、按出现顺序）。
        """
        if env_file is None or not env_file.is_file():
            return []
        known = {name.lower() for name in cls.model_fields}
        encoding = str(cls.model_config.get("env_file_encoding") or "utf-8")
        try:
            text = env_file.read_text(encoding=encoding)
        except (OSError, UnicodeDecodeError) as exc:
            # 读不了就跳过检查：这条提示的价值远小于「因为读不到 .env 而拒绝启动」。
            logger.warning("读取配置文件失败，跳过未知键检查：%s（%s）", env_file, exc)
            return []

        unknown: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            # WHY 大小写不敏感：``case_sensitive=False`` 下 ``db_path`` 与 ``DB_PATH`` 等价，
            # 把它们判成未知键会是纯噪音（而噪音会让这条提示被忽略）。
            if key and key.lower() not in known and key not in unknown:
                unknown.append(key)
        return unknown


MountKind = Literal["file", "dir"]
"""挂载对象的类型：单个文件（父目录不可见），或整棵目录树（写一律拒绝）。"""


@dataclass(frozen=True)
class VirtualMount:
    """一个**只读**挂进虚拟文件系统的宿主路径。

    Attributes:
        prefix: 虚拟路径前缀（如 ``/global/``、``/skills/``）；``CompositeBackend``
            按它路由，并在转发前**剥掉**它。
        host_path: 宿主上的真实路径（文件或目录）。
        label: 错误文案与日志里对它的说法（如「全局长期记忆」「技能库」）。
        kind: ``file`` 只暴露那一个文件（父目录里的其它文件不可见）；``dir`` 暴露整棵
            子树，但写操作一律拒绝。
    """

    prefix: str
    host_path: Path
    label: str
    kind: MountKind = "file"

    def __post_init__(self) -> None:
        """校验取值，避免拼出一个路由不到、或文案说不清的挂载点。

        WHY 校验前缀形态：``CompositeBackend`` 按前缀匹配并**剥掉**它再转发，前缀写错
        （少一个斜杠）会落进默认 backend——于是这个文件连同路径语义都跑到工作区里去了，
        而装配与运行都不会报错。

        Raises:
            ValueError: 前缀不是 ``/`` 开头 ``/`` 结尾、``host_path`` 不是 ``Path``、
                ``kind`` 不是 ``file`` / ``dir``、文件挂载没有文件名、``label`` 为空。
        """
        if not self.prefix.startswith("/") or not self.prefix.endswith("/"):
            raise ValueError(f"挂载前缀必须以 / 开头并以 / 结尾：{self.prefix!r}")
        if not isinstance(self.host_path, Path):
            raise ValueError(f"host_path 必须是 Path，实际：{type(self.host_path).__name__}")
        if self.kind not in ("file", "dir"):
            raise ValueError(f"kind 只能是 file 或 dir，实际：{self.kind!r}")
        if self.kind == "file" and not self.host_path.name:
            raise ValueError(f"host_path 必须指向一个文件（有文件名）：{self.host_path}")
        if not self.label or not str(self.label).strip():
            raise ValueError("label 不能为空：错误文案要靠它说明这是哪一种挂载")

    @property
    def virtual_path(self) -> str:
        """该路径在虚拟文件系统里的位置：文件含文件名，目录就是前缀本身。"""
        if self.kind == "file":
            return f"{self.prefix}{self.host_path.name}"
        return self.prefix.rstrip("/") or "/"


@dataclass(frozen=True)
class MemoryPlan:
    """本会话的长期记忆方案：根内的来源 + 需要挂载的根外文件。

    WHY 把两者放进同一个对象、且 ``sources`` 由挂载**派生**：``sources`` 里的
    ``/global/AGENTS.md`` 只有在 ``mounts`` 里挂了对应文件时才读得到，而 deepagents 对
    读不到的来源是**静默跳过**的（只留一行 WARNING）。分成两个方法返回（一个给来源、
    一个给挂载），调用方漏接一半就会得到「记忆少了一条」，且没有任何错误指向它。

    Attributes:
        in_root_sources: 落在会话根内、无需挂载就能读到的来源（如工作区自带的
            ``/AGENTS.md``）。
        mounts: 需要只读挂进虚拟文件系统的根外文件。
    """

    in_root_sources: list[str]
    mounts: list[VirtualMount]

    @property
    def sources(self) -> list[str]:
        """deepagents ``memory`` 参数所需的来源列表（挂载出来的在前）。"""
        return [mount.virtual_path for mount in self.mounts] + list(self.in_root_sources)

    @property
    def mounted_files(self) -> list[Path]:
        """当前挂载的宿主文件（供日志与排错使用）。"""
        return [mount.host_path for mount in self.mounts]


@dataclass(frozen=True)
class SessionRoot:
    """一条会话的**文件根**：配置 + 具体根目录，附上全部由它派生的路径。

    根只有两种来源，本模型下没有第三种：

    1. **用户绑定的工作空间**——用户在创建会话时选定的目录，可以同时容纳多条会话；
    2. **会话专属目录**——没绑定工作空间的会话，在 ``SESSIONS_ROOT`` 下按会话 ID
       自动创建的子目录，只有它自己用。

    两者在本对象看来完全一样（一个绝对路径），因为文件工具、沙箱挂载根、附件目录与
    知识库索引需要的都只是「一个根」。区分它们的地方只有两处：解析时（谁决定这个根）
    与界面文案（「工作空间」还是「会话专属目录」）。

    WHY 单独成为一个对象，而不是给 ``AppConfig`` 的那些方法各加一个 ``root`` 参数：

    1. **漏传必须炸，而不是静默退回某个默认值**。「用错了根」是这套设计里最危险的
       失败形态（写进别人的项目、把附件的根指错、技能视图建到另一个目录）。参数若带
       默认值，任何一处漏传都会悄悄用另一个目录，而症状（文件跑到别处）与原因相距
       极远；把「哪个根」收进一个必须显式获得的对象里，漏了就是 ``AttributeError``。
    2. **派生逻辑只有一份**。长期记忆路径、技能来源与虚拟路径映射全都依赖根目录，
       把它们挂在这里是唯一能保证「同一个根处处得到同一组派生结果」的形态。
    3. **可独立测试**：它只依赖 ``AppConfig`` 的字段，不需要启动任何服务。

    Attributes:
        config: 应用配置（提供上限、显式覆盖项等与根无关的取值）。
        root: 根目录的绝对路径。
    """

    config: AppConfig
    root: Path

    def __post_init__(self) -> None:
        """把根归一到绝对路径。

        WHY 这里**不**校验目录是否存在：会话专属目录在第一次用到它之前并不存在，而
        「存在性」这件事对用户绑定的工作空间必须严格（拼错的路径不能静默变成一个空
        目录）。两者放在同一个入口判定会二选一牺牲一边，因此校验落在**用户输入进
        来的那一刻**——由应用层的解析函数负责（见 ``SessionRegistry.resolve``），
        而本对象只需保证「拿到的是一个绝对路径」，目录按需创建（``ensure_directories``）。

        Raises:
            ValueError: ``config`` 为 ``None``。
        """
        if self.config is None:
            raise ValueError("config 不能为 None")
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    # ------------------------------------------------------------------ 基本属性

    @property
    def global_memory_file(self) -> Path | None:
        """**全局**长期记忆文件的绝对路径；未配置时为 ``None``。

        WHY 它不受会话根约束：这份文件按定义就是跨会话的（人工维护的偏好与约定），
        要求它落在工作区内等于要求「每条会话各放一份」。旧实现把它当成「工作区之外 →
        跳过」，于是配了全局记忆的实例里**所有**会话都静默少一条记忆——只有一行
        WARNING，用户看不出自己写的东西没生效。

        Returns:
            ``MEMORY_FILE`` 的绝对路径（字段校验已展开 ``~`` 并转绝对路径）；
            未配置时 ``None``。
        """
        return self.config.memory_file

    @property
    def workspace_memory_file(self) -> Path:
        """本工作区自带的 ``AGENTS.md``（未配置全局记忆时才作为来源）。

        WHY 仍然保留这份回落：工作区里的 ``AGENTS.md`` 是**项目自己的**说明（不与其它
        工作区共享），而「没配全局记忆」的实例正是最需要它的场合。
        """
        return self.root / _MEMORY_FILE_NAME

    @cached_property
    def memory_plan(self) -> MemoryPlan:
        """本会话的长期记忆方案：来源与为它们准备的只读挂载点。

        WHY 缓存（``cached_property``）：它要读磁盘、还会记日志，而同一次装配里 backend
        的路由与 ``memory`` 参数都要取它——算两遍会把同一件事记两遍，也会让「文件恰好
        在这一刻被删」出现两种结果。``SessionRoot`` 是**按请求**构造的，所以缓存期限就是
        一次请求，不会让人工刚改完的内容滞留。

        WHY 配置了全局记忆就不再加载工作区自带的那份：一份记忆只应有一个来源，否则两处
        说法冲突时没有任何优先级依据（deepagents 只是把它们一起塞进提示），用户很难理解
        「我改的是全局那份，Agent 却不照做」。它若存在会记一行 INFO，免得用户以为生效了。

        Returns:
            来源列表与挂载点；两者缺一时读不到对应来源（deepagents 会静默跳过），
            因此它们由同一个对象给出，见 :class:`MemoryPlan`。
        """
        global_file = self.global_memory_file
        if global_file is None:
            # 没配全局记忆：退回到「本工作区自带的 AGENTS.md」。
            if not self.workspace_memory_file.is_file():
                logger.warning("长期记忆文件不存在，跳过加载：%s", self.workspace_memory_file)
                return MemoryPlan(in_root_sources=[], mounts=[])
            return MemoryPlan(
                in_root_sources=[f"/{self.workspace_memory_file.name}"], mounts=[]
            )

        if self.workspace_memory_file.is_file():
            logger.info(
                "已配置全局长期记忆（%s）：工作区自带的 %s 不再作为来源",
                global_file,
                self.workspace_memory_file,
            )
        if not global_file.is_file():
            logger.warning("全局长期记忆文件不存在，跳过加载：%s", global_file)
            return MemoryPlan(in_root_sources=[], mounts=[])

        mount = VirtualMount(
            prefix=GLOBAL_MEMORY_PREFIX, host_path=global_file, label="全局长期记忆"
        )
        logger.info("全局长期记忆将以只读方式挂载：%s ← %s", mount.virtual_path, global_file)
        return MemoryPlan(in_root_sources=[], mounts=[mount])

    # ------------------------------------------------------------------ 根外存储

    @cached_property
    def storage_dir(self) -> Path:
        """本根的**根外存储目录**：技能库、技能视图与工具留存都住在这里。

        WHY 把它们搬出工作区（2026-09-21 改）：工作区可能是**用户的仓库**，而这三个名字
        是应用自己的数据——往别人的项目里写东西会污染他的版本控制，也会让「这次操作到底
        动了什么」变得说不清。搬出来之后，工作区里只剩用户自己的文件与 Agent 的产物。

        WHY 位置由**根路径**派生、而不是按会话分：同一个工作空间可能承载多条会话，而
        「这个工作空间装了什么技能」本来就该共享（技能启停状态也是全应用一份，见
        ``runtime.skill_store``）。按会话分会让同一工作空间的第二条会话看不到第一条装好
        的技能——那正是「技能库」这个概念要消除的重复。

        WHY 目录名 = 可读片段 + 短哈希：纯哈希在排障时无法对应回工作区（``roots/`` 下
        十几个随机名），纯可读名会重名（两台机器上都有 ``Desktop``）。两者都要。

        Note:
            ``SessionRoot`` 是**按请求**构造的，所以这个缓存只活一次请求——换工作区
            会换对象，不会拿到别人的存储目录。
        """
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:12]
        return self.config.roots_store_root / f"{_readable_dir_name(self.root)}-{digest}"

    @property
    def skills_store(self) -> Path:
        """技能库目录（用户放技能包的地方）。"""
        return self.storage_dir / _SKILLS_STORE_DIR_NAME

    @property
    def skill_view_store(self) -> Path:
        """技能视图目录（只把启用中的技能物化出来的派生物）。"""
        return self.storage_dir / _SKILL_VIEW_STORE_DIR_NAME

    @property
    def tool_output_store(self) -> Path:
        """工具输出留存目录（``<store>/<会话 ID>/NNNN-<工具>.txt``）。"""
        return self.storage_dir / _TOOL_OUTPUTS_STORE_DIR_NAME

    @cached_property
    def read_only_mounts(self) -> list[VirtualMount]:
        """本根要挂进虚拟文件系统的**全部**根外路径（只读）。

        WHY 收成一个列表、且与「技能来源」同源：图的技能来源与文件工具看到的路径都靠
        这些挂载才解析得到。分开给（一个给来源、一个给挂载）时，漏接一半的表现是
        「面板里列得出来、Agent 却读不到」，而 deepagents 对读不到的技能来源只是记一条
        警告——没有任何错误会指向装配漏了一条路由。

        挂载清单：
        - ``/skills/``：技能库（人工维护）；
        - ``/.skills-active/``：技能视图（派生物，建图时的来源就是它）；
        - ``/_tool_outputs/``：工具输出留存（消息里只带引用，正文在这里）；
        - 内置技能与 ``SKILL_DIRS`` 指定的目录：它们位于应用目录或任意位置，同样必须
          挂上——``sources_for_graph`` 的兜底（视图缺失 → 退回配置目录）正是靠它们才
          真的读得到，否则那句「等于全部启用」是假的。
        """
        planned: list[VirtualMount] = [
            VirtualMount(
                prefix=f"{VIRTUAL_SKILL_VIEW}/",
                host_path=self.skill_view_store,
                label="技能视图",
                kind="dir",
            ),
            VirtualMount(
                prefix=f"{VIRTUAL_TOOL_OUTPUTS}/",
                host_path=self.tool_output_store,
                label="工具输出留存",
                kind="dir",
            ),
        ]
        seen = {mount.prefix for mount in planned}
        # 技能库只在「没显式配置 SKILL_DIRS」时挂我们自己那个存储目录：显式配置时技能库
        # **就是那些目录**（与 ``skill_dir_plan`` 同一个分支）。两个都挂同一前缀会让后来者
        # 被静默跳过——症状是「面板里技能一个都列不出来」，而目录里明明有技能包。
        if not self.config.skill_dirs:
            planned.append(
                VirtualMount(
                    prefix=f"{VIRTUAL_SKILLS}/",
                    host_path=self.skills_store,
                    label="技能库",
                    kind="dir",
                )
            )
            seen.add(f"{VIRTUAL_SKILLS}/")
        for source in self.skill_dir_plan():
            prefix = f"{source.virtual}/"
            if prefix in seen:
                continue
            planned.append(
                VirtualMount(
                    prefix=prefix,
                    host_path=source.host_dir,
                    label=f"技能来源 {source.virtual}",
                    kind="dir",
                )
            )
            seen.add(prefix)
        return planned

    @property
    def mount_table(self) -> dict[str, Path]:
        """``{虚拟前缀: 宿主目录}``：只含**目录**挂载，供巡检与路径解析用。"""
        return {
            mount.prefix: mount.host_path
            for mount in self.read_only_mounts
            if mount.kind == "dir"
        }

    @property
    def skill_view_virtual(self) -> str:
        """技能视图的虚拟路径（建图时的技能来源就是它）。

        WHY 由这里给出而不是让调用方各自 import 常量：虚拟路径是**挂载约定**的一半
        （另一半是 ``read_only_mounts`` 里的前缀），两者必须同源——分开写会在改动其一
        之后留下「来源指向没人挂的路径」这种静默失效。
        """
        return VIRTUAL_SKILL_VIEW

    @property
    def tool_outputs_virtual(self) -> str:
        """工具输出留存的虚拟路径前缀（消息与事件里的引用以它开头）。"""
        return VIRTUAL_TOOL_OUTPUTS

    def ensure_storage(self) -> None:
        """建好根外存储目录，并把旧位置（工作区里的 ``skills/``）的技能库搬过来。

        WHY 要迁移而不是「重新开始」：技能包是**用户放进来的东西**，静默丢掉等于让用户
        的技能凭空消失——而现场表现只是「Agent 忽然不会那些套路了」，没有任何报错指向
        这里。迁移是幂等的：新位置已有内容就不再动旧位置。

        WHY 只迁技能库、不迁另外两个：技能视图是派生物（重建即可），工具留存是「最近若干
        次工具输出」的缓存（``TOOL_OUTPUT_RETENTION_PER_THREAD`` 会自然淘汰），都不值得
        为它们写迁移；技能包丢了却是真丢了。

        Raises:
            OSError: 目录创建失败（磁盘满、权限不足）。
        """
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        # WHY 不预建技能视图目录：``sources_for_graph`` 用「视图目录是否存在」判断
        # 「物化视图是否已建过」——预建一个空目录会让那个判据永远为真，于是「视图被清理 /
        # 重建失败」与「所有技能都停用」变得无法区分（前者会让技能静默消失且没有告警）。
        # 视图由 ``runtime.skill_view.rebuild_view`` 自己建（先建临时目录再整体替换）。
        self.tool_output_store.mkdir(parents=True, exist_ok=True)
        if self.config.skill_dirs:
            # 显式配置了技能目录：技能库**就是那些目录**，我们的存储里那份不被使用。
            # 此时既不建它、更不能把 ``<根>/skills`` 当「旧位置」搬走——那可能是用户自己
            # 配进去的路径（搬走会让配置里的路径凭空消失，技能一个都列不出来）。
            return
        self.skills_store.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_skills()

    def _migrate_legacy_skills(self) -> None:
        """把工作区里的 ``skills/``（旧位置）搬进存储目录；只剩空目录时顺手清掉。"""
        legacy = self.root / _USER_SKILLS_DIR_NAME
        if not legacy.is_dir():
            return
        try:
            children = list(legacy.iterdir())
        except OSError as exc:
            logger.warning("读取旧技能库失败，保持原样：%s（%s）", legacy, exc)
            return

        if not children:
            # WHY 空目录也删：它是旧版本由应用建出来的；留着会让用户看到「说搬走了却还在」，
            # 而那个目录对他没有任何用途。
            try:
                legacy.rmdir()
            except OSError as exc:
                logger.debug("旧技能库是空目录，删除失败（无害）：%s（%s）", legacy, exc)
            return

        if self.skills_store.is_dir() and any(self.skills_store.iterdir()):
            logger.warning(
                "旧技能库 %s 里还有内容，而新位置 %s 已非空：两边都保留，请自行合并",
                legacy,
                self.skills_store,
            )
            return

        try:
            for child in children:
                shutil.move(str(child), str(self.skills_store / child.name))
            legacy.rmdir()
        except OSError as exc:
            # WHY 出错就停手、且不动旧位置：搬一半会让技能分散在两处，而调用方无法判断
            # 哪边是全的——宁可让用户看到一个完整的旧目录。
            logger.error(
                "迁移旧技能库失败，旧位置仍保留（请手工搬到 %s）：%s（%s）",
                self.skills_store,
                legacy,
                exc,
            )
            return
        logger.info("旧技能库已搬到根外存储：%s → %s", legacy, self.skills_store)

    @property
    def skill_dirs(self) -> list[Path]:
        """本工作区生效的技能目录（宿主机路径）；技能库在根外存储里。"""
        return [source.host_dir for source in self.skill_dir_plan()]

    # ------------------------------------------------------------------ 目录准备

    def ensure_directories(self) -> None:
        """创建本工作区运行所需的目录。

        WHY 在工作区**被选中的那一刻**建，而不是启动时替所有候选目录建：后者会往
        用户还没用过的项目里写目录，而那是用户的仓库，不是我们的。

        WHY 现在只建**根本身**：技能库、技能视图与工具留存都已经搬到根外存储
        （``ensure_storage``），而长期记忆由人工维护（应用不替它建目录——那等于往工作区
        之外悄悄写目录）。根内不再有任何「应用自己的目录」，所以这里只保证根存在。
        """
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 技能来源

    def skill_dir_plan(self) -> list[SkillSource]:
        """技能目录 → 虚拟路径的完整规划（含根外与共享来源）。

        WHY 虚拟路径不再由「是否在工作区内」推导：技能库已经搬到工作区之外，而内置技能
        本来就在外面——两者的虚拟路径只能由**挂载表**决定（挂在哪，就按哪读）。按位置
        推导（旧规则：工作区外取目录名）在两种来源共存时会给出与真实挂载点不一致的路径，
        而症状是「面板里列得出来、Agent 却读不到」。

        Returns:
            来源列表，顺序即优先级（越靠后优先级越高）。
        """
        if self.config.skill_dirs:
            # 显式配置优先：用户说了算，虚拟路径取目录名（与 ``read_only_mounts`` 的挂载
            # 前缀一致，见那里的 WHY）。
            return [
                SkillSource(host_dir=directory, virtual=f"/{directory.name}")
                for directory in self.config.skill_dirs
            ]
        return [
            SkillSource(host_dir=BUILTIN_SKILLS_DIR, virtual=VIRTUAL_BUILTIN_SKILLS),
            SkillSource(host_dir=self.skills_store, virtual=VIRTUAL_SKILLS),
        ]

    def skill_sources(self) -> list[SkillSource]:
        """把**存在**的技能目录映射成「宿主机目录 + 虚拟路径」的来源列表。

        WHY 必须显式映射：技能由 ``SkillsMiddleware`` 经 backend 读取，而 backend 只认
        虚拟路径——目录挂在哪，就从哪读（见 ``read_only_mounts``）。

        WHY 先 ``ensure_storage()``：技能库是我们的目录，读来源之前确保它就位（幂等），
        否则「刷新页面直接打开一条历史会话」这条路径上（它只装配图、不装配面板服务）
        会看到「技能库不存在」的 WARNING，而那条警告是假的。

        Returns:
            来源列表，顺序即优先级（越靠后优先级越高）。

        Raises:
            ValueError: 两个技能目录映射到同一个虚拟路径——静默的后果是其中一个
                来源整体失效，而界面上看不出任何异常。
        """
        self.ensure_storage()
        sources: list[SkillSource] = []
        claimed: dict[str, Path] = {}
        for source in self.skill_dir_plan():
            if not source.host_dir.is_dir():
                # WHY 过滤而非报错：技能库是可选能力，缺失只应降级而不是让应用无法启动。
                logger.warning(
                    "技能目录不存在，已跳过：%s（虚拟路径 %s）", source.host_dir, source.virtual
                )
                continue
            previous = claimed.get(source.virtual)
            if previous is not None:
                msg = (
                    f"技能目录 {previous} 与 {source.host_dir} 映射到同一个虚拟路径 "
                    f"{source.virtual}；请给其中一个改名，或用 SKILL_DIRS 显式指定不同的目录"
                )
                logger.error("%s", msg)
                raise ValueError(msg)
            claimed[source.virtual] = source.host_dir
            sources.append(source)
        return sources

    def skill_source_paths(self) -> list[str]:
        """返回 deepagents ``skills`` 参数所需的虚拟路径列表（``/skills`` 之类）。

        WHY 必须是虚拟路径而不是宿主机绝对路径：backend 只认虚拟路径，绝对路径（Windows
        下还带盘符与反斜杠）在虚拟模式下会被当成工作区内的子路径，导致技能永远命中不了。
        这些虚拟路径由 ``read_only_mounts`` 挂上，两者必须同源。
        """
        return [source.virtual for source in self.skill_sources()]

    def skill_host_dir(
        self, virtual_directory: str, *, sources: list[SkillSource] | None = None
    ) -> Path | None:
        """把技能（或技能目录）的虚拟路径还原成宿主机路径；无法归属时返回 ``None``。

        WHY 需要反向映射：技能物化视图要从**真实目录**复制内容（见
        ``runtime.skill_view.rebuild_view``），而巡检结果给的是虚拟路径。内置技能在
        工作区之外，直接与工作区拼接会得到一个不存在的路径——表现为「技能在清单里、
        却怎么也复制不进视图」，而视图为空又会让该技能静默失效。

        Args:
            virtual_directory: 技能目录的虚拟路径，如 ``/skills/code-review``。
            sources: 已经算好的来源列表；``None`` 表示就地求一次。批量解析（视图重建）
                必须传入，否则每个技能都要重新扫一遍技能目录。

        Returns:
            宿主机绝对路径；该路径不属于任何已知来源时为 ``None``。
        """
        normalized = "/" + str(virtual_directory).strip().strip("/")
        best_length = -1
        best: Path | None = None
        for source in self.skill_sources() if sources is None else sources:
            if normalized == source.virtual:
                candidate = source.host_dir
            elif normalized.startswith(source.virtual + "/"):
                candidate = source.host_dir / normalized[len(source.virtual) + 1 :]
            else:
                continue
            # WHY 取最长匹配：来源可以嵌套（``/skills`` 与 ``/skills/team``），取短的
            # 那个会把团队技能映射到基础目录下，而它的源目录根本不是那里。
            if len(source.virtual) > best_length:
                best_length = len(source.virtual)
                best = candidate
        return best

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志排错
        return f"SessionRoot(root={self.root})"


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """进程内共享同一份配置。

    WHY 缓存：Web 服务每个请求都会用到配置，重复解析 ``.env`` 既浪费 IO，
    也可能导致同一进程内出现两份不一致的路径。
    """
    return AppConfig.load()
