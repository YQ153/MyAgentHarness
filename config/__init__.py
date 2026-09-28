"""统一配置中心（包形态的门面）。

所有环境变量、路径与运行时参数只在 ``config/`` 包内声明与校验，避免散落到
各个模块后出现多处重复解析、校验口径不一致的问题。

读取优先级：环境变量 > .env 文件 > 字段默认值。

包内布局：

- ``constants``：目录名、虚拟挂载前缀与默认白名单（包内最底层）；
- ``parsing``：列表配置解析、目录名清洗与回环地址判定（纯函数）；
- ``enums``：执行 / 沙箱 / MCP 传输 / 嵌入后端档位枚举；
- ``settings.*``：按功能域拆分的字段 mixin（模型 / 路径 / 工作区 / 执行与
  沙箱 / 治理 / 工具扩展 / 检索与语义）；
- ``app_config``：``AppConfig`` 组合根与跨域方法、``get_config`` 进程级句柄；
- ``session_root``：``SessionRoot`` / ``VirtualMount`` / ``MemoryPlan`` 等会话
  根上的派生对象。

WHY 本文件做纯 re-export 而不放任何逻辑：仓库里 70+ 处调用点都以
``from config import X`` 取用配置层，门面让它们在拆包后**一行不改**；同时
它是对外的唯一窗口——包内以下划线开头的名字（目录名常量、工具函数）不
出现在这里，天然对包外不可见。

WHY 显式 ``__all__`` 而不是靠 import 副作用：ruff（F401）据此放行 re-export，
更重要的是它就是本包的公共 API 清单——新增公共名时必须在此登记，漏登记
会在第一次被包外导入时以 ``ImportError`` 暴露，而不是静默失败。
"""

from __future__ import annotations

from config.app_config import AppConfig, get_config
from config.constants import (
    BUILTIN_SKILLS_DIR,
    DEFAULT_ATTACHMENT_MIME_TYPES,
    DEFAULT_ENV_ALLOWLIST,
    GLOBAL_MEMORY_PREFIX,
    HARNESS_DIR_NAME,
    NETWORK_MODE_HOST,
    NETWORK_MODE_NONE,
    PRESET_SKILLS_DIR,
    VIRTUAL_BUILTIN_SKILLS,
    VIRTUAL_HARNESS,
    VIRTUAL_PRESET_SKILLS,
    VIRTUAL_SKILLS,
    VIRTUAL_SKILL_VIEW,
    VIRTUAL_TOOL_OUTPUTS,
)
from config.enums import EmbeddingBackendKind, ExecutionMode, MCPTransport, SandboxTier
from config.parsing import is_loopback_host, parse_list_config
from config.session_root import (
    MemoryPlan,
    MountKind,
    SessionRoot,
    SkillSource,
    VirtualMount,
)
from config.settings.tools import MCPServerSpec

__all__ = [
    # 模型
    "AppConfig",
    "get_config",
    # 常量
    "BUILTIN_SKILLS_DIR",
    "DEFAULT_ATTACHMENT_MIME_TYPES",
    "DEFAULT_ENV_ALLOWLIST",
    "GLOBAL_MEMORY_PREFIX",
    "HARNESS_DIR_NAME",
    "NETWORK_MODE_HOST",
    "NETWORK_MODE_NONE",
    "PRESET_SKILLS_DIR",
    "VIRTUAL_BUILTIN_SKILLS",
    "VIRTUAL_HARNESS",
    "VIRTUAL_PRESET_SKILLS",
    "VIRTUAL_SKILLS",
    "VIRTUAL_SKILL_VIEW",
    "VIRTUAL_TOOL_OUTPUTS",
    # 解析与判定
    "is_loopback_host",
    "parse_list_config",
    # 枚举
    "EmbeddingBackendKind",
    "ExecutionMode",
    "MCPTransport",
    "SandboxTier",
    # 模型与规格
    "MCPServerSpec",
    # 会话根
    "MemoryPlan",
    "MountKind",
    "SessionRoot",
    "SkillSource",
    "VirtualMount",
]
