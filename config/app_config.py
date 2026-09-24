"""AppConfig：应用配置的组合根与跨域逻辑。

``AppConfig`` 本体由 7 个域 mixin（``config.settings.*``）与 ``BaseSettings``
组合而成；本文件只保留**跨域**的内容：

- ``model_config``（pydantic-settings 的加载行为，只属于最终组合类）；
- 进程级方法与派生属性（``load`` / ``warn_unknown_env_keys`` /
  ``warn_if_publicly_exposed`` / ``session_dir`` / 目录准备等）——它们要读
  多个域的字段或对外代表整个配置对象，放在任何一个域 mixin 里都会造成
  「域越界」。

原 ``config.AppConfig`` 的全部字段与单域 validator 已按分区迁入
``config/settings/*``，语义逐字不变；``.env`` 加载、类型转换与错误口径由
pydantic-settings 在组合类上统一生效。
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from config.constants import _ROOTS_STORE_DIR_NAME, _SESSIONS_DIR_NAME, PRESET_SKILLS_DIR
from config.parsing import is_loopback_host
from config.settings.execution import ExecutionSettings
from config.settings.governance import GovernanceSettings
from config.settings.llm import LlmSettings
from config.settings.paths import PathsSettings
from config.settings.retrieval import RetrievalSettings
from config.settings.tools import MCPServerSpec, ToolsSettings
from config.settings.workspace import WorkspaceSettings

logger = logging.getLogger(__name__)


class AppConfig(
    LlmSettings,
    PathsSettings,
    WorkspaceSettings,
    ExecutionSettings,
    GovernanceSettings,
    ToolsSettings,
    RetrievalSettings,
    BaseSettings,
):
    """应用配置。

    使用 ``pydantic-settings`` 而非裸 ``os.getenv``：字段的类型转换、
    缺省值与非法值拦截由框架统一处理，调用方拿到的永远是可信对象。

    字段按功能域声明在 ``config/settings/*`` 的各 mixin 里（模型 / 路径 /
    工作区 / 执行与沙箱 / 治理 / 工具扩展 / 检索与语义），本类通过多继承
    把它们合并为一个模型——``model_fields`` 因此包含全部域的字段，
    ``.env`` 对账（``tests/test_env_example_contract.py``）与未知键告警
    （``warn_unknown_env_keys``）都按合并后的全集工作。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    def warn_if_publicly_exposed(self, effective_host: str | None = None) -> bool:
        """绑定非回环地址时，输出 ERROR 级告警。

        WHY 告警而不是拒绝启动：本项目默认绑定 ``127.0.0.1``，硬拒绝会让
        「改绑内网地址做联调」这类正当场景被误伤；要消除的只是「静默」——
        服务不再区分调用方，一旦它对非本机可达，任何能访问该地址的客户端都能
        直接使用 ``execute`` 等工具，操作者必须有机会看见这件事。

        WHY 接受 ``effective_host`` 覆盖：``python main.py web --host`` 的
        命令行参数优先于配置，若只读 ``self.host``，加一个 ``--host 0.0.0.0``
        就能绕过本检查——检查必须盯住「最终真正绑定的地址」。

        Args:
            effective_host: 实际生效的监听地址；``None`` 表示取 ``self.host``。

        Returns:
            True 表示已发出告警（绑定非回环）；False 表示无需告警（绑定回环）。

        Raises:
            ValueError: ``effective_host`` 既非 ``None`` 也非字符串。
        """
        if effective_host is not None and not isinstance(effective_host, str):
            msg = f"effective_host 必须是字符串或 None，实际：{type(effective_host).__name__}"
            logger.error("%s", msg)
            raise ValueError(msg)

        bind_host = self.host if effective_host is None else effective_host
        if is_loopback_host(bind_host):
            return False

        logger.error(
            "对外暴露风险：监听地址 %r 不是本机回环——任何能访问该地址的客户端"
            "都可直接使用本服务（含 execute 工具）；请改绑 127.0.0.1，"
            "或在有访问控制的网络边界之后运行",
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
        """**旧版**根外存储的父目录：``<数据目录>/roots``。

        WHY 还留着：2026-09-22 起技能库 / 技能视图 / 工具留存已经搬回工作区内的
        ``.harness/``（见 ``SessionRoot.storage_dir``），但升级路径上必须能找到旧位置并把
        数据搬过去（``SessionRoot.legacy_root_store_dir``）。这个属性**只服务于迁移**：
        新代码不应再往它下面写任何东西。

        WHY 仍由数据目录派生：它曾经要求与数据目录同卷，这样「备份/搬迁只搬数据目录」才
        成立；保留同一派生规则，迁移来源的定位就与旧版本逐字一致。
        """
        return self.db_path.parent / _ROOTS_STORE_DIR_NAME

    @property
    def skill_presets_dir(self) -> Path:
        """场景预设目录：配置给了就用它，否则 ``<应用目录>/skills/presets``。

        WHY 由配置暴露、而不是让调用方各自 import 常量：预设目录是「技能从哪来」这一事实的
        一半，而它同时被技能服务（建视图时按场景过滤）与面板（列出可选场景）读取。两处各写
        一遍常量，一旦常量被改名或将来加了环境变量覆盖，两处就会看到不同的世界。
        """
        return self.presets_dir or PRESET_SKILLS_DIR

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


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """进程内共享同一份配置。

    WHY 缓存：Web 服务每个请求都会用到配置，重复解析 ``.env`` 既浪费 IO，
    也可能导致同一进程内出现两份不一致的路径。
    """
    return AppConfig.load()
