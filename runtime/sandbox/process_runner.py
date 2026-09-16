"""Tier 0：宿主机进程沙箱。

隔离强度是本档位的天花板——它**不是**安全边界，只是把「命令失控」的代价
限制在可恢复范围内：

- Windows：Job Object 提供进程树管控、活动进程数上限、内存/CPU 上限，
  并保证超时后整棵树被消灭（``KILL_ON_JOB_CLOSE``）；
- POSIX：进程组 + ``SIGKILL``，只能保证超时终止，资源上限留待 cgroups 版本。

因此本档位必须与 HITL 人工审批配合使用——参见 ``agent/guardrails.py``。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

from config import SandboxTier
from runtime.sandbox.errors import SandboxError, SandboxPolicyError
from runtime.sandbox.models import CommandRequest, CommandResult, SandboxPolicy

if TYPE_CHECKING:
    from collections.abc import Mapping

if sys.platform == "win32":
    # WHY 直接导入子模块而非 ``from runtime.sandbox import _winjob``：
    # 本模块是被 ``runtime/sandbox/__init__.py`` 间接导入的，此时包对象尚在
    # 初始化中，按包属性取子模块有取不到的风险。
    from runtime.sandbox._winjob import can_use_jobs, run_in_job

logger = logging.getLogger(__name__)

_TIMEOUT_EXIT_CODE = 124
"""超时退出码，与 ``LocalShellBackend`` 的约定保持一致。"""


class ProcessSandboxRunner:
    """在宿主进程内执行命令并施加资源管控。"""

    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        """构造 runner。

        Args:
            policy: 资源与隔离策略；``None`` 时使用默认策略。
        """
        self._policy = policy or SandboxPolicy()
        self._is_windows = sys.platform == "win32"

    @property
    def tier(self) -> SandboxTier:
        """本 runner 的沙箱档位。"""
        return SandboxTier.PROCESS

    @property
    def policy(self) -> SandboxPolicy:
        """当前生效的策略（只读）。"""
        return self._policy

    def probe(self) -> bool:
        """探测本档位是否可用。

        Windows 下必须真的能创建 Job Object——否则进程树管控形同虚设，
        而这正是本档位存在的理由。
        """
        if self._is_windows:
            return can_use_jobs()
        return True

    def describe(self) -> str:
        """返回一行档位说明。"""
        if self._is_windows:
            return (
                "Tier 0 进程沙箱（Windows Job Object：进程树管控、"
                f"进程数<={self._policy.max_processes}、"
                f"内存<={self._policy.max_memory_mb}MB、"
                f"CPU<={self._policy.cpu_percent}%）"
            )
        return "Tier 0 进程沙箱（POSIX 进程组：仅保证超时终止，无资源上限）"

    def close(self) -> None:
        """释放资源；本 runner 无持久资源，空实现以保持接口一致。"""

    def run(self, request: CommandRequest) -> CommandResult:
        """执行命令。

        Args:
            request: 命令请求。

        Returns:
            执行结果；命令自身失败体现为退出码，不抛异常。

        Raises:
            SandboxPolicyError: 请求违反策略。
            SandboxError: 沙箱自身故障（如无法创建 Job Object）。
        """
        self._validate(request)
        env = self._prepare_env(request)

        try:
            if self._is_windows:
                result = run_in_job(
                    request.command,
                    cwd=request.cwd,
                    env=env,
                    timeout=request.timeout,
                    policy=self._policy,
                )
                stdout, stderr, exit_code, timed_out = result
            else:
                stdout, stderr, exit_code, timed_out = self._run_posix(request, env)
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001
            # WHY 兜住所有异常：沙箱故障不该以堆栈形式炸进 Agent 调用栈，
            # 统一转成带上下文的 SandboxError，由 backend 变成用户可读的消息。
            logger.exception("沙箱执行失败：command=%s cwd=%s", request.command, request.cwd)
            msg = f"沙箱执行失败（{type(exc).__name__}）：{exc}"
            raise SandboxError(msg) from exc

        stdout_text, stdout_clipped = _clip(stdout, request.max_output_bytes)
        stderr_text, stderr_clipped = _clip(stderr, request.max_output_bytes)

        return CommandResult(
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=exit_code,
            truncated=stdout_clipped or stderr_clipped,
            timed_out=timed_out,
            tier=self.tier,
        )

    def _validate(self, request: CommandRequest) -> None:
        """校验请求与工作目录。"""
        if not isinstance(request, CommandRequest):
            msg = f"request 必须是 CommandRequest，当前为 {type(request).__name__}"
            raise SandboxPolicyError(msg)
        if not request.cwd.is_dir():
            msg = f"工作目录不存在或不是目录：{request.cwd}"
            raise SandboxPolicyError(msg)

    def _prepare_env(self, request: CommandRequest) -> dict[str, str]:
        """构造子进程环境：白名单过滤 + 按需剔除网络变量。"""
        # WHY 委托给策略对象：WSL 档位需要完全相同的清洗口径，重复实现会让
        # 两个档位的「哪些变量能进沙箱」随时间漂移出一个危险差值。
        return self._policy.child_env(request.env)

    def _run_posix(
        self,
        request: CommandRequest,
        env: Mapping[str, str],
    ) -> tuple[str, str, int | None, bool]:
        """POSIX 分支：自成进程组，超时向整组发送 ``SIGKILL``。"""
        # noqa 说明：shell=True 是本档位的设计前提——命令来自 LLM 的自由文本。
        with subprocess.Popen(  # noqa: S602
            request.command,
            shell=True,
            cwd=str(request.cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=request.timeout)
            except subprocess.TimeoutExpired:
                self._terminate_group(process, request.timeout)
                # WHY 再收一次：进程被杀后缓冲区里已有输出仍要交还模型，
                # 否则超时命令的「死前现场」全部丢失，排查无从下手。
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
                return stdout or "", stderr or "", _TIMEOUT_EXIT_CODE, True
            return stdout or "", stderr or "", process.returncode, False

    def _terminate_group(self, process: subprocess.Popen[str], timeout: int) -> None:
        """终止整个进程组。

        WHY 用 ``killpg`` 而非 ``process.kill()``：后者只杀直接子进程，命令
        fork 出的孙进程会残留；``start_new_session`` 已让子进程自成一组的
        组号等于其 pid，据此整组消灭。
        """
        logger.warning("命令执行超时（%ss），终止整个进程组", timeout)
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError, PermissionError) as exc:
            logger.warning("终止进程组失败，回退为终止主进程：%s", exc)
            process.kill()


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """按字符数截断文本。

    WHY 按字符而非字节：输出交给模型前已经是 ``str``，按字节截断会在多字节
    字符中间切断，产生替换符乱码。

    WHY 不在此处拼接提示语：本层是基础设施，提示文案属于「模型看到的输出
    格式」，由 backend 统一负责，两处都加会出现重复提示。
    """
    if len(text) <= limit:
        return text, False
    return text[:limit], True
