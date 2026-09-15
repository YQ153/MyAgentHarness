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

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class ExecutionMode(StrEnum):
    """``execute`` 工具的执行档位。

    local:    宿主机直跑 shell，仅限本机开发，与 ``LocalShellBackend`` 的安全
              警告一致——绝不可用于 Web 或多租户环境。
    sandbox:  容器内执行，需要外部沙盒实现，尚未接入。
    disabled: 使用非沙盒后端，``execute`` 工具仍存在但调用后返回错误；
              这是默认档位，保证进程上线即处于安全状态。
    """

    LOCAL = "local"
    SANDBOX = "sandbox"
    DISABLED = "disabled"


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

    # ---------------- 护栏 ----------------
    max_model_calls_per_run: int = Field(default=60, gt=0)
    recursion_limit: int = Field(default=100, gt=0)

    # ---------------- HTTP 服务 ----------------
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"

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
            "配置加载完成：model=%s mode=%s workspace=%s",
            instance.default_model,
            instance.execution_mode,
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
