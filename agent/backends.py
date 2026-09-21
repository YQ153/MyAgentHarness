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
from deepagents.backends.protocol import BackendProtocol, ExecuteResponse

from agent.path_safety import ExtendedPathSafeBackendMixin
from agent.readonly_mount import ReadOnlyDirectoryMount, ReadOnlyFileMount
from agent.run_context import namespace_of_runtime
from agent.sandbox_backend import SandboxedFilesystemBackend
from runtime.host_shell import HostShellExecutor
from runtime.sandbox import build_sandbox_runner

if TYPE_CHECKING:
    from pathlib import Path

    from langgraph.store.base import BaseStore

    from config import AppConfig, SessionRoot

logger = logging.getLogger(__name__)

_TIMEOUT_EXIT_CODE = 124
"""超时退出码，与 ``LocalShellBackend`` 及沙箱 runner 的约定保持一致。"""


def _combine_output(stdout: str, stderr: str, max_output_bytes: int) -> tuple[str, bool]:
    """合并 stdout/stderr 并按阈值截断。

    WHY 复刻上游的合并格式：``[stderr]`` 前缀与 ``<no output>`` 占位是模型
    已经学会解读的约定，换执行器时改动它会让既有提示语与输出对不上。

    Args:
        stdout: 标准输出。
        stderr: 标准错误。
        max_output_bytes: 输出上限（字符数，沿用上游口径）。

    Returns:
        ``(合并后的输出, 是否发生截断)``。
    """
    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.extend(f"[stderr] {line}" for line in stderr.strip().split("\n"))
    output = "\n".join(parts) if parts else "<no output>"

    if len(output) > max_output_bytes:
        clipped = output[:max_output_bytes]
        return f"{clipped}\n\n... Output truncated at {max_output_bytes} bytes.", True
    return output, False


class _ExtendedPathSafeLocalShellBackend(ExtendedPathSafeBackendMixin, LocalShellBackend):
    """local 档位：混入 Windows 扩展前缀容错，并接管命令执行。

    WHY 不直接用 LocalShellBackend：并行 write_file 新建目录时
    ``Path.resolve()`` 可能返回 ``\\\\?\\`` 前缀路径，官方越界校验会误报
    （见 agent/path_safety.py 的故障复盘）。

    WHY 重写 ``execute`` 而不是沿用上游实现：上游用 ``subprocess.run(timeout=)``，
    实测（T2 风险验证）在 Windows 上超时只终止直接子进程 ``cmd.exe``，孙进程
    连同管道写端一起存活，``execute()`` 会阻塞到它们自己退出——「停止」因此
    只是接口层确认。``HostShellExecutor`` 自管进程树，超时与中止都能杀整棵。
    """

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """在宿主机上执行一条 shell 命令。

        输出格式与上游 ``LocalShellBackend.execute`` 保持一致（stderr 行加
        ``[stderr]`` 前缀、空输出写 ``<no output>``、非零退出码追加说明），
        以避免更换执行器导致模型看到的输出语义发生变化。

        Args:
            command: shell 命令原文。
            timeout: 本次超时秒数；``None`` 用构造时的默认值。

        Returns:
            ``ExecuteResponse``；命令失败体现为退出码，不抛异常。

        Raises:
            ValueError: 超时值非正（与上游一致，便于调用方统一处理）。
        """
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1,
                truncated=False,
            )

        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            msg = f"timeout must be positive, got {effective_timeout}"
            raise ValueError(msg)

        try:
            result = HostShellExecutor().run(
                command,
                cwd=self.cwd,
                env=getattr(self, "_env", {}) or {},
                timeout=effective_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            # WHY 与上游一致地兜住一切异常：工具层抛异常会打断整轮图执行，
            # 而命令失败本就是 Agent 需要看到并自行调整的常见结果。
            logger.exception("local 档位命令执行失败：%s", command)
            return ExecuteResponse(
                output=f"Error executing command ({type(exc).__name__}): {exc}",
                exit_code=1,
                truncated=False,
            )

        output, truncated = _combine_output(result.stdout, result.stderr, self._max_output_bytes)
        if result.timed_out:
            # WHY 超时也带上已回收的输出：上游只回一句「超时了」，而恰恰是
            # 「死前现场」能告诉模型该改什么（例如在等用户输入、卡在某个包）。
            output = (
                f"Error: Command timed out after {effective_timeout} seconds "
                f"and was terminated.\n\n{output}".strip()
            )
            return ExecuteResponse(output=output, exit_code=_TIMEOUT_EXIT_CODE, truncated=truncated)
        if result.exit_code:
            output = f"{output.rstrip()}\n\nExit code: {result.exit_code}"
        return ExecuteResponse(
            output=output,
            exit_code=result.exit_code or 0,
            truncated=truncated,
        )


class _ExtendedPathSafeFilesystemBackend(ExtendedPathSafeBackendMixin, FilesystemBackend):
    """disabled 档位：混入 Windows 扩展前缀容错，理由同上。"""

_MEMORY_NAMESPACE = namespace_of_runtime
"""/memories/ 路由的存储命名空间工厂。

WHY 按主体而不是全局固定前缀：Store 是共享空间，全局前缀意味着开了鉴权之后
B 用户的 Agent 能读到 A 用户写下的记忆——跨用户泄漏，而不是「记忆没隔离」
这种体验问题。主体由 ``AgentRunContext`` 经 LangGraph 的 ``Runtime.context``
传进来（见 ``agent.run_context``）。"""


def build_backend(
    config: AppConfig, store: BaseStore, *, scope: SessionRoot
) -> CompositeBackend:
    """构造 CompositeBackend：``/memories/`` 走持久化、全局记忆只读挂载，其余走给定工作区。

    WHY 必须拆分：长期记忆要跨会话存活，工作文件要与用户磁盘一致，
    两者生命周期不同，单一 Backend 无法同时满足。

    WHY 工作区由 ``scope`` 显式传入而不是从 ``config`` 取：工作区已经是会话级取值，
    而 backend 的 ``root_dir`` 一旦定下就决定了 Agent 能读写宿主的哪片目录。沿用
    「配置里那一个」会让某条会话的文件操作落到另一个项目的目录里，且没有任何提示。

    WHY 全局记忆要单独挂载（``scope.memory_plan.mounts``）：它在任何工作区之外，而
    backend 只认那个根。不挂载的后果不是报错，而是 deepagents 静默跳过该来源——用户
    写下的全局约定对全部会话都不生效，且只有一行 WARNING。挂载点只暴露那一个文件，
    见 ``agent.readonly_mount``。

    Args:
        config: 应用配置，提供执行档位与资源上限。
        store: 长期记忆存储；``/memories/`` 路由依赖它。
        scope: 本会话生效的**文件根**（用户选的工作空间，或应用为它建的专属目录）。

    Raises:
        ValueError: ``config`` / ``store`` / ``scope`` 为 ``None``。
        NotADirectoryError: 文件根不是一个已存在的目录。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    if store is None:
        raise ValueError("store 不能为 None，/memories/ 路由依赖它")
    if scope is None:
        raise ValueError("scope 不能为 None：backend 的根由它决定")

    workspace: Path = scope.root
    if not workspace.is_dir():
        raise NotADirectoryError(f"文件根不是一个有效目录：{workspace}")

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
        runner = build_sandbox_runner(config, workspace=workspace)
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

    routes: dict[str, BackendProtocol] = {
        "/memories/": StoreBackend(namespace=_MEMORY_NAMESPACE, store=store)
    }
    # 根外路径按声明挂成**只读**路由。WHY 从 scope 取而不是从 config 读：「哪些来源真的
    # 存在」是**会话级**判定（工作区自带的 AGENTS.md 随根变化），而 memory / skills 参数
    # 用的也是同一份数据——两处各算一次必然漂开，漂开的表现正是「来源列了、挂载没建」，
    # 于是记忆静默少一条、技能一个都加载不到。
    #
    # 三类路由的来源不同：
    # - 长期记忆（``memory_plan.mounts``，单文件）：人工维护、跨会话共享；
    # - 根外存储（``read_only_mounts``，整棵目录）：技能库 / 技能视图 / 工具输出留存，
    #   它们在 2026-09-21 之前住在工作区里，现在搬到了 ``<数据目录>/roots/…``；
    # - 技能来源里的根外目录（内置技能、``SKILL_DIRS`` 指定的目录）同样由
    #   ``read_only_mounts`` 带出来——视图缺失时的兜底路径要靠它们才真的读得到。
    for mount in [*scope.memory_plan.mounts, *scope.read_only_mounts]:
        if mount.kind == "dir":
            routes[mount.prefix] = ReadOnlyDirectoryMount(mount.host_path, label=mount.label)
        else:
            routes[mount.prefix] = ReadOnlyFileMount(
                mount.host_path, label=mount.label, mounted_at=mount.virtual_path
            )
        logger.info(
            "只读挂载已装配：%s ← %s（%s）", mount.virtual_path, mount.host_path, mount.label
        )

    return CompositeBackend(default=default, routes=routes)
