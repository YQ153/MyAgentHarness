"""Backend 构造：按执行档位决定「文件落在哪、命令在哪跑」。

三档位的设计依据是 ``LocalShellBackend`` 源码中的安全警告——它明确写出
「不适用于生产环境、Web 服务、多租户系统」，因此 Web 场景不能复用 CLI 的
宿主直跑策略。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from deepagents.backends import (
    CompositeBackend,
    FilesystemBackend,
    LocalShellBackend,
    StoreBackend,
)

from agent.path_safety import ExtendedPathSafeBackendMixin
from agent.sandbox_backend import SandboxedFilesystemBackend
from runtime.sandbox import build_sandbox_runner

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from pathlib import Path

    from langgraph.store.base import BaseStore

    from config import AppConfig

logger = logging.getLogger(__name__)


class _ExtendedPathSafeLocalShellBackend(ExtendedPathSafeBackendMixin, LocalShellBackend):
    """local 档位：混入 Windows 扩展前缀容错。

    WHY 不直接用 LocalShellBackend：并行 write_file 新建目录时
    ``Path.resolve()`` 可能返回 ``\\\\?\\`` 前缀路径，官方越界校验会误报
    （见 agent/path_safety.py 的故障复盘）。
    """


class _ExtendedPathSafeFilesystemBackend(ExtendedPathSafeBackendMixin, FilesystemBackend):
    """disabled 档位：混入 Windows 扩展前缀容错，理由同上。"""

_MEMORY_NAMESPACE = lambda rt: ("memories",)  # noqa: E731
"""/memories/ 路由的存储命名空间。

按固定前缀隔离，避免与其他业务的 Store 数据相互污染。
"""


def build_backend(config: AppConfig, store: BaseStore) -> CompositeBackend:
    """构造 CompositeBackend：``/memories/`` 走持久化，其余走工作区。

    WHY 必须拆分：长期记忆要跨会话存活，工作文件要与用户磁盘一致，
    两者生命周期不同，单一 Backend 无法同时满足。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    if store is None:
        raise ValueError("store 不能为 None，/memories/ 路由依赖它")

    workspace: Path = config.workspace
    if not workspace.is_dir():
        raise NotADirectoryError(f"工作区不是有效目录：{workspace}")

    default: BackendProtocol
    if config.execution_mode.value == "local":
        # WHY 仅本机开发使用：无隔离、无资源限制，命令以当前用户权限直接作用于宿主机
        logger.warning(
            "执行档位=local：Agent 可在本机执行任意 shell 命令，禁止用于 Web 环境"
        )
        default = _ExtendedPathSafeLocalShellBackend(
            root_dir=str(workspace),
            virtual_mode=True,
            timeout=config.shell_timeout,
            max_output_bytes=config.shell_max_output_bytes,
            # WHY 不继承环境变量：宿主机环境变量常含 API Key，会顺着子进程泄漏
            inherit_env=False,
        )
    elif config.execution_mode.value == "sandbox":
        # WHY 由 factory 装配 runner：档位是否可用由能力探测决定，装配失败
        # 直接抛出，绝不退化到 LocalShellBackend——静默降级会让使用者误以为
        # 命令跑在隔离环境里。
        runner = build_sandbox_runner(config)
        default = SandboxedFilesystemBackend(
            root_dir=str(workspace),
            runner=runner,
            virtual_mode=True,
            timeout=config.sandbox_timeout,
            max_output_bytes=config.sandbox_max_output_bytes,
            # WHY 不传 env：环境变量清洗交给 runner 的策略白名单统一负责，
            # 在两处各维护一份白名单必然不一致。
            inherit_env=False,
        )
        logger.warning(
            "执行档位=sandbox（%s）：该档位只做资源管控，不是安全边界，"
            "必须与人工审批配合使用",
            default.describe(),
        )
    else:
        # WHY disabled 走普通 FilesystemBackend：非沙盒后端不满足
        # SandboxBackendProtocol，execute 工具调用时会直接返回错误，
        # 正好实现「工具存在但不可用」，模型也能据此调整策略。
        logger.info("执行档位=disabled：execute 工具调用将返回错误")
        default = _ExtendedPathSafeFilesystemBackend(root_dir=str(workspace))

    return CompositeBackend(
        default=default,
        routes={"/memories/": StoreBackend(namespace=_MEMORY_NAMESPACE, store=store)},
    )
