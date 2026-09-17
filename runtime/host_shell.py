"""宿主机命令执行：自管进程树，超时/中止都能杀整棵树。

WHY 存在这一层（T2 风险验证的实测结论）：``local`` 档位原先走
``subprocess.run(shell=True, timeout=...)``，在 Windows 上超时只终止直接子进程
``cmd.exe``，孙进程由父进程 ``Popen`` 启动并继承了管道写端；CPython 的超时分支是
「``kill()`` + **无超时** ``communicate()``」，于是 ``execute()`` 会阻塞到写端
关闭为止——命令实际继续跑完自己的寿命，``stop`` 之后的「已停止」只是接口层确认。

本模块因此自己管进程：

- Windows：交给 Job Object（``runtime/sandbox/_winjob.run_in_job``，
  ``policy=None`` 表示只做进程树管控），输出重定向到文件而非管道；
- POSIX：``start_new_session`` 自成进程组，超时用 ``killpg(SIGKILL)`` 整组消灭。

两条路径都会在执行期间登记一个中止句柄（``runtime/execution_registry``），
使「停止运行」能立即终止进程树，而不必等到超时。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from runtime import execution_registry

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_TIMEOUT_EXIT_CODE = 124
"""超时退出码，与 ``LocalShellBackend`` / ``ProcessSandboxRunner`` 的约定一致。"""

_GRACE_SECONDS = 5
"""终止进程树后回收残余输出的等待上限。

WHY 必须有上限：``communicate()`` 不带超时会阻塞到**所有持有管道写端的进程**
退出，而那些进程正是我们刚刚杀不掉时才残留的——不加限制就等于把「超时」
又变回「无限等待」，这正是要修的问题本身。
"""


class HostShellResult(NamedTuple):
    """一次宿主机命令执行的原始结果。"""

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


class HostShellExecutor:
    """在宿主机上执行命令，并保证超时/中止时整棵进程树被消灭。

    本类无状态：每次执行所需的一切都由 ``run`` 的参数给出，因此可以安全地在
    多个工作线程间共享同一个实例。
    """

    def run(
        self,
        command: str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int,
    ) -> HostShellResult:
        """执行一条 shell 命令。

        Args:
            command: shell 命令原文（由模型产出，故 ``shell=True`` 是前提）。
            cwd: 子进程工作目录。
            env: 子进程环境；调用方负责清洗。
            timeout: 超时秒数，必须为正数。

        Returns:
            执行结果；输出不做截断（截断是上层的表示层职责）。

        Raises:
            ValueError: 参数非法。
            OSError: 进程创建失败（Windows API 或 fork 失败）。
            RuntimeError: Windows 下进程树管控不可用（宁可失败也不静默降级）。
        """
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command 必须是非空字符串")
        if not isinstance(cwd, Path):
            raise ValueError("cwd 必须是 Path")
        if not cwd.is_dir():
            raise ValueError(f"工作目录不存在或不是目录：{cwd}")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("timeout 必须是正整数")

        if sys.platform == "win32":
            # WHY 延迟导入：``runtime.sandbox`` 包会拽出 factory → process_runner
            # → 本模块，而在模块顶层就导入它会形成环（本模块 → sandbox →
            # 本模块）。放到调用点即可，因为只有真正执行时才需要它。
            from runtime.sandbox._winjob import run_in_job

            stdout, stderr, exit_code, timed_out = run_in_job(
                command,
                cwd=cwd,
                env=env,
                timeout=timeout,
                policy=None,
            )
            return HostShellResult(stdout, stderr, exit_code, timed_out)
        return self._run_posix(command, cwd=cwd, env=env, timeout=timeout)

    def _run_posix(
        self,
        command: str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int,
    ) -> HostShellResult:
        """POSIX 分支：自成进程组，超时向整组发送 ``SIGKILL``。"""
        # noqa 说明：shell=True 是本档位的设计前提——命令来自 LLM 的自由文本。
        with subprocess.Popen(  # noqa: S602
            command,
            shell=True,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            abort_handle = _ProcessGroupHandle(process)
            registered = execution_registry.register(abort_handle)
            try:
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self._terminate_group(process, timeout)
                    # WHY 再收一次：进程被杀后缓冲区里已有输出仍要交还模型，
                    # 否则超时命令的「死前现场」全部丢失，排查无从下手。
                    try:
                        stdout, stderr = process.communicate(timeout=_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        stdout, stderr = "", ""
                    return HostShellResult(stdout or "", stderr or "", _TIMEOUT_EXIT_CODE, True)
            finally:
                # WHY 无论如何都要关闭管道：孙进程继承了写端，只要还有一个
                # 写端打开，后续任何读取都会挂住；这里显式关掉，剩下的事
                # 交给已经被杀的进程自己收尾。
                _close_pipes(process)
                if registered:
                    execution_registry.unregister(abort_handle)
            return HostShellResult(stdout or "", stderr or "", process.returncode, False)

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


class _ProcessGroupHandle:
    """POSIX 进程组的中止句柄。"""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process

    def abort(self) -> None:
        """向整个进程组发送 ``SIGKILL``；进程已退出时无害。"""
        logger.warning("收到中止请求，终止命令进程组")
        try:
            os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError, PermissionError) as exc:
            logger.warning("终止进程组失败：%s", exc)


def _close_pipes(process: subprocess.Popen[str]) -> None:
    """关闭子进程的三条管道，忽略一切失败。

    WHY 逐个关闭并吞错：进程已退出时 ``stdin`` 可能已关闭，而仍在运行的孙进程
    持有写端会让 ``stdout.close()`` 抛错——清理阶段抛错会掩盖真正的执行结果。
    """
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            logger.debug("关闭子进程管道失败", exc_info=True)
