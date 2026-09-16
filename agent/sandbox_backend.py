"""沙箱 backend：文件走工作区，命令走 ``SandboxRunner``。

WHY 继承 ``LocalShellBackend`` 而非从 ``FilesystemBackend`` 重新组合：

1. 它已经是官方的「``FilesystemBackend`` + ``SandboxBackendProtocol``」组合式，
   直接复用即可拿到 ``id``、环境清洗、超时与输出截断的全部参数；
2. 框架在 ``middleware/filesystem.py`` 里用 ``isinstance(default, LocalShellBackend)``
   判断 ``/memories/`` 等路由是否可从 shell 访问——换成自己拼的子类会让这段
   行为静默改变（路由被描述为「shell 不可达」）；
3. ``isinstance(default, SandboxBackendProtocol)`` 是 ``execute`` 工具的注册
   条件，继承链天然满足。

本类只替换一件事：``execute()`` 从「宿主 ``subprocess``」改为「委托 runner」。
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse
from agent.path_safety import ExtendedPathSafeBackendMixin
from runtime.sandbox.errors import SandboxError
from runtime.sandbox.models import CommandRequest

if TYPE_CHECKING:
    from pathlib import Path

    from runtime.sandbox.models import CommandResult
    from runtime.sandbox.protocol import SandboxRunner

logger = logging.getLogger(__name__)

_EMPTY_OUTPUT = "<no output>"
"""空输出占位；与 ``LocalShellBackend`` 的约定一致，避免模型误判为执行失败。"""


class SandboxedFilesystemBackend(ExtendedPathSafeBackendMixin, LocalShellBackend):
    """把命令执行委托给沙箱 runner 的文件系统 backend。

    ``ExtendedPathSafeBackendMixin`` 置于 MRO 首位：只覆盖
    ``_resolve_path`` / ``_to_virtual_path`` 消除 Windows ``\\\\?\\`` 扩展
    前缀在并行新建目录时的越界误报，不影响下述 isinstance 语义。
    """

    def __init__(
        self,
        root_dir: str | Path | None = None,
        *,
        runner: SandboxRunner,
        timeout: int = 120,
        max_output_bytes: int = 100_000,
        env: dict[str, str] | None = None,
        inherit_env: bool = False,
        virtual_mode: bool = True,
    ) -> None:
        """构造 backend。

        Args:
            root_dir: 工作区根目录，同时作为命令执行的 ``cwd``。
            runner: 沙箱执行器；命令全部经它执行。
            timeout: 默认命令超时秒数。
            max_output_bytes: 输出截断阈值（stdout / stderr 各自计）。
            env: 传入子进程的环境变量；``None`` 时由 runner 按策略白名单清洗。
            inherit_env: 是否继承宿主环境；默认 ``False`` 以防凭据泄漏。
            virtual_mode: 是否启用虚拟根路径模式。

        Raises:
            ValueError: ``runner`` 为 ``None``。
        """
        if runner is None:
            msg = "runner 不能为 None：沙箱 backend 必须绑定一个执行器"
            raise ValueError(msg)

        super().__init__(
            root_dir=root_dir,
            virtual_mode=virtual_mode,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            env=env,
            inherit_env=inherit_env,
        )
        self._runner = runner
        self._sandbox_id = f"sandbox-{runner.tier.value}-{uuid.uuid4().hex[:8]}"

    @property
    def id(self) -> str:  # noqa: A003
        """带档位标识的实例 ID，便于日志与追踪区分隔离等级。"""
        return self._sandbox_id

    @property
    def runner(self) -> SandboxRunner:
        """当前绑定的沙箱执行器（只读）。"""
        return self._runner

    def describe(self) -> str:
        """返回一行档位说明，供启动日志与降级提示使用。"""
        return self._runner.describe()

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """在沙箱内执行一条 shell 命令。

        Args:
            command: shell 命令原文。
            timeout: 本次执行的超时秒数；``None`` 使用构造时的默认值。

        Returns:
            ``ExecuteResponse``；命令失败体现为 ``exit_code`` 而非异常——
            模型需要读到输出自行决策，抛异常会中断整轮对话。

        Raises:
            ValueError: ``timeout`` 不是正数。
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

        request = CommandRequest(
            command=command,
            cwd=self.cwd,
            timeout=effective_timeout,
            max_output_bytes=self._max_output_bytes,
            # WHY 环境为空时传 None：交给 runner 按策略白名单从宿主环境挑，
            # 直接传空 dict 会让子进程连 PATH/COMSPEC 都没有，命令必然失败。
            env=self._env or None,
        )

        try:
            result = self._runner.run(request)
        except SandboxError as exc:
            # WHY 沙箱故障也转成 ExecuteResponse：一次沙箱故障不该炸掉整轮
            # 对话，但必须留下日志——静默返回成功会让用户以为命令跑了。
            logger.exception("沙箱执行失败：command=%s", command)
            return ExecuteResponse(
                output=f"Error executing command ({type(exc).__name__}): {exc}",
                exit_code=1,
                truncated=False,
            )

        logger.info(
            "沙箱执行完成：tier=%s exit=%s timed_out=%s truncated=%s",
            result.tier.value,
            result.exit_code,
            result.timed_out,
            result.truncated,
        )
        return ExecuteResponse(
            output=self._render_output(result, effective_timeout),
            exit_code=result.exit_code,
            truncated=result.truncated,
        )

    def _render_output(self, result: CommandResult, timeout: int) -> str:
        """按 ``LocalShellBackend`` 的输出约定渲染结果。

        WHY 严格对齐官方格式：模型对「``[stderr]`` 前缀」「``Exit code: N``」
        这些信号已形成稳定预期，换个格式等于换一套交互协议。
        """
        parts: list[str] = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            # 每行加前缀，让 stderr 与 stdout 混排后仍可区分来源
            parts.extend(f"[stderr] {line}" for line in result.stderr.strip().split("\n"))
        if result.timed_out:
            parts.append(f"Error: Command timed out after {timeout} seconds.")

        output = "\n".join(parts) if parts else _EMPTY_OUTPUT

        if result.truncated:
            output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
        if result.exit_code:
            output = f"{output.rstrip()}\n\nExit code: {result.exit_code}"
        return output
