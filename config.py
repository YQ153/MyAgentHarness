"""统一配置中心。

所有环境变量、路径与运行时参数只在此处声明与校验，避免散落到各个模块后
出现多处重复解析、校验口径不一致的问题。

读取优先级：环境变量 > .env 文件 > 字段默认值。
"""

from __future__ import annotations

import logging
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    deepseek_api_key: str = ""
    deepseek_api_base: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    default_model: str = "deepseek-flash"

    openai_api_key: str = ""
    """OpenAI 的 API Key；留空则不注册 ``openai`` 别名。"""

    openai_api_base: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    anthropic_api_key: str = ""
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
    skill_dirs: list[Path] = Field(default_factory=lambda: [Path("./workspace/skills")])

    # ---------------- 会话 ----------------
    thread_title_max_chars: int = Field(default=24, ge=1, le=200)
    """会话列表中标题的字符上限。

    WHY 做成配置：不同前端宽度能承载的标题长度不同，硬编码会让窄侧栏溢出、
    宽侧栏浪费空间；而这里只约束「截断长度」，不参与任何存储结构。
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

    sandbox_env_allowlist: list[str] = Field(default_factory=lambda: list(DEFAULT_ENV_ALLOWLIST))
    """允许传入子进程的环境变量白名单。

    WHY 白名单：宿主机环境常含 ``*_API_KEY``、云凭证、``USERPROFILE``，
    黑名单补不全；命令真正需要的变量只有固定的少数几个。
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

    auth_session_secret: str = ""
    """本地会话 Cookie 签名密钥；auth_mode != disabled 时必须提供且不少于 32 字节。"""

    auth_cookie_name: str = "harness_session"
    auth_session_max_age_seconds: int = Field(default=28800, ge=60)
    auth_cookie_secure: bool = False
    """Cookie 的 Secure 标志；生产环境必须设为 ``True`` 并配合 HTTPS。"""
    auth_cookie_samesite: str = Field(default="lax", pattern="^(lax|strict|none)$")
    """Cookie 的 SameSite 属性；OIDC 回调需要浏览器带 Cookie，默认 ``lax``。"""

    # API Key 模式
    auth_api_key_header: str = "X-API-Key"
    auth_api_key_dev: str = ""
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

    # 认证端点限流
    auth_rate_limit_window_seconds: int = Field(default=60, ge=1)
    auth_rate_limit_max_attempts: int = Field(default=10, ge=1)
    """单 IP 在窗口内允许的最大认证请求数（login/callback/apikey 校验）。"""

    # OIDC 模式
    oidc_issuer: str = ""
    """IdP 的 issuer URL，例如 https://auth.example.com/application/o/myagentharness/。"""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
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
