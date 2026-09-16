"""Windows Job Object 沙箱的 ctypes 封装。

WHY 手写 ctypes 而不是引入 pywin32：项目要求 Python >= 3.14，第三方扩展
未必及时提供对应 wheel；而这里只用到 ``kernel32`` 的 8 个函数与 3 个结构体，
标准库足以覆盖，且不会给 Linux/macOS 部署带来额外依赖。

本模块只做一件事：**创建受管控的进程、等待其结束、并确保超时后整棵进程树
被彻底消灭**。命令语义、输出截断、错误处理等由上层 runner 负责。
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Mapping

    from runtime.sandbox.models import SandboxPolicy

logger = logging.getLogger(__name__)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

# ---------------- 常量 ----------------
CREATE_SUSPENDED = 0x0000_0004
CREATE_BREAKAWAY_FROM_JOB = 0x0100_0000
CREATE_UNICODE_ENVIRONMENT = 0x0000_0400
CREATE_NO_WINDOW = 0x0800_0000
STARTF_USESTDHANDLES = 0x0000_0100

WAIT_OBJECT_0 = 0x0000_0000
WAIT_TIMEOUT = 0x0000_0102
WAIT_FAILED = 0xFFFF_FFFF
MAX_WAIT_MS = 0xFFFF_FFFE

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x0000_2000
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x0000_0008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0000_0100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x0000_0200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0000_0400

JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x0000_0001
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x0000_0004

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS = 15

ERROR_ACCESS_DENIED = 5
ERROR_NOT_SUPPORTED = 50

EXIT_CODE_TIMEOUT = 124
"""超时退出码，与 ``LocalShellBackend`` 的约定保持一致。"""


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    """``CpuRate`` 以 1/100 百分比为单位，故 50% 记为 5000。"""

    _fields_ = [
        ("ControlFlags", wintypes.DWORD),
        ("CpuRate", wintypes.DWORD),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


# 显式声明签名：64 位下句柄是 8 字节，缺 argtypes 会被当成 int 截断。
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateJobObject.restype = wintypes.BOOL
_kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(_STARTUPINFOW),
    ctypes.POINTER(_PROCESS_INFORMATION),
]
_kernel32.CreateProcessW.restype = wintypes.BOOL
_kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
_kernel32.ResumeThread.restype = wintypes.DWORD
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class JobResult(NamedTuple):
    """``run_in_job`` 的原始结果。"""

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


def can_use_jobs() -> bool:
    """探测本机是否允许创建 Job Object（绝大多数 Windows 都可以）。"""
    handle = _kernel32.CreateJobObjectW(None, None)
    if not handle:
        logger.warning("无法创建 Job Object：%s", ctypes.WinError(ctypes.get_last_error()))  # type: ignore[attr-defined]
        return False
    _kernel32.CloseHandle(handle)
    return True


def run_in_job(
    command: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    policy: SandboxPolicy,
) -> JobResult:
    """在 Job Object 中执行一条命令。

    Args:
        command: 待执行的 shell 命令。
        cwd: 子进程工作目录。
        env: 已清洗的环境变量（调用方负责按白名单过滤）。
        timeout: 超时秒数。
        policy: 资源限制策略。

    Returns:
        ``JobResult``，输出为已解码的文本，不做截断。

    Raises:
        OSError: Windows API 调用失败（由 ``ctypes.WinError`` 抛出）。
        RuntimeError: 无法把进程加入 Job Object——此时继续运行等于放弃进程树
            管控，宁可失败也不能静默降级。
    """
    job = _create_job_object(policy)
    # WHY 临时目录在本层创建而非下沉到 ``_spawn_and_wait``：它必须在「进程树
    # 已终止」之后才清理。命令可以派生后台进程（``start /b``、``pythonw``），
    # 那些进程会继承日志文件句柄；若先删目录再关 Job，删除必然被 WinError 32
    # 挡下，还会把一次本已成功的执行渲染成 SandboxError。
    tmp_dir = tempfile.mkdtemp(prefix="sandbox-")
    try:
        return _spawn_and_wait(
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            job=job,
            tmp_dir=Path(tmp_dir),
        )
    finally:
        # WHY 先显式终止进程树再关句柄：``KILL_ON_JOB_CLOSE`` 的生效时机绑定
        # 在句柄关闭上，而 ``TerminateJobObject`` 立即生效、句柄随之释放，
        # 后面删临时文件才不会被占用。
        if not _kernel32.TerminateJobObject(job, 1):
            logger.debug(
                "TerminateJobObject 调用失败（错误码 %s），进程可能已退出",
                ctypes.get_last_error(),  # type: ignore[attr-defined]
            )
        # 句柄一关，KILL_ON_JOB_CLOSE 兜底终止残余——「不留残留」的落点，
        # 异常路径同样必须执行。
        _kernel32.CloseHandle(job)
        _cleanup_dir(tmp_dir)


def _create_job_object(policy: SandboxPolicy) -> wintypes.HANDLE:
    """创建并配置 Job Object。"""
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]

    limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    )
    limits.BasicLimitInformation.LimitFlags = flags
    limits.BasicLimitInformation.ActiveProcessLimit = policy.max_processes
    process_memory = policy.max_memory_mb * 1024 * 1024
    limits.ProcessMemoryLimit = process_memory
    # WHY Job 内存给到单进程的两倍：Job 上限按「同一时刻峰值」计，一次性
    # 命令常出现短暂的父子进程并存，取等值会被误杀。
    limits.JobMemoryLimit = process_memory * 2

    if not _kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        code = ctypes.get_last_error()  # type: ignore[attr-defined]
        _kernel32.CloseHandle(job)
        raise ctypes.WinError(code)  # type: ignore[attr-defined]

    _apply_cpu_limit(job, policy)
    return job


def _apply_cpu_limit(job: wintypes.HANDLE, policy: SandboxPolicy) -> None:
    """设置 CPU 硬上限。

    WHY 失败只告警不抛出：CPU 限流在部分环境（无该 Information Class、Job
    嵌套）下不可用，而它只是加固项——因为少了它就拒绝执行，代价与收益不成比例。
    """
    cpu = _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
    cpu.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
    cpu.CpuRate = policy.cpu_percent * 100
    if not _kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS,
        ctypes.byref(cpu),
        ctypes.sizeof(cpu),
    ):
        logger.warning(
            "CPU 限流不可用（错误码 %s），将以无 CPU 上限方式执行",
            ctypes.get_last_error(),  # type: ignore[attr-defined]
        )


def _spawn_and_wait(
    command: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    job: wintypes.HANDLE,
    tmp_dir: Path,
) -> JobResult:
    """创建挂起进程、入 Job、恢复执行并等待结束。

    临时目录由调用方提供并在进程树终止后清理，本函数不负责删除。
    """
    stdout_path = tmp_dir / "stdout.log"
    stderr_path = tmp_dir / "stderr.log"

    with (
        open(stdout_path, "w+b") as stdout_file,  # noqa: PTH123
        open(stderr_path, "w+b") as stderr_file,  # noqa: PTH123
        open(os.devnull, "rb") as devnull,  # noqa: PTH123, SIM115
    ):
        # WHY 重定向到文件而非管道：管道缓冲写满会让子进程阻塞写、父进程
        # 阻塞读，形成死锁；文件重定向没有这个问题，截断也只需读前 N 字节。
        stdout_handle = _inheritable_handle(stdout_file)
        stderr_handle = _inheritable_handle(stderr_file)
        stdin_handle = _inheritable_handle(devnull)

        process, thread = _create_suspended_process(
            command,
            cwd=cwd,
            env=env,
            stdin_handle=stdin_handle,
            stdout_handle=stdout_handle,
            stderr_handle=stderr_handle,
        )
        try:
            # WHY 必须「先挂起、入 Job、再恢复」：若先运行再指派，命令
            # 可能在指派前就 fork 出子进程，那些孙进程将脱离 Job 管控。
            if not _kernel32.AssignProcessToJobObject(job, process):
                code = ctypes.get_last_error()  # type: ignore[attr-defined]
                _kernel32.TerminateProcess(process, 1)
                _kernel32.CloseHandle(process)
                logger.error("无法将进程加入 Job Object（错误码 %s），已终止该进程", code)
                msg = f"进程树管控不可用（错误码 {code}），已拒绝执行以避免失控进程残留"
                raise RuntimeError(msg)
            _kernel32.ResumeThread(thread)
        finally:
            _kernel32.CloseHandle(thread)

        try:
            timed_out = _wait_for_exit(process, job, timeout)
            exit_code = _resolve_exit_code(process, timed_out)
        finally:
            _kernel32.CloseHandle(process)

    # WHY 在句柄全部关闭后才读取：文件内容已落盘，此时读取既完整又不会与
    # 仍在写的孙进程竞争。
    return JobResult(
        stdout=_read_text(stdout_path),
        stderr=_read_text(stderr_path),
        exit_code=exit_code,
        timed_out=timed_out,
    )


def _cleanup_dir(path: str) -> None:
    """删除沙箱临时目录，失败仅告警。

    WHY 绝不让清理失败影响执行结果：命令本身已经跑完、输出也读到了，为了
    ``%TEMP%`` 里一个残留目录把整次执行报成失败，是典型的「清理逻辑反过来
    决定业务结果」。带短暂重试是因为进程终止与句柄释放之间存在极小的时间差。
    """
    delay = 0.05
    for attempt in range(3):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            if attempt == 2:
                logger.warning("沙箱临时目录清理失败，将在系统清理时回收：%s（%s）", path, exc)
                return
            time.sleep(delay)
            delay *= 2


def _inheritable_handle(file_object: object) -> int:
    """取可被子进程继承的 OS 句柄。"""
    import msvcrt

    handle = msvcrt.get_osfhandle(file_object.fileno())  # type: ignore[attr-defined]
    os.set_handle_inheritable(handle, True)
    return handle


def _create_suspended_process(
    command: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin_handle: int,
    stdout_handle: int,
    stderr_handle: int,
) -> tuple[wintypes.HANDLE, wintypes.HANDLE]:
    """以挂起状态创建进程，返回 ``(进程句柄, 主线程句柄)``。"""
    startup = _STARTUPINFOW()
    startup.cb = ctypes.sizeof(_STARTUPINFOW)
    startup.dwFlags = STARTF_USESTDHANDLES
    startup.hStdInput = stdin_handle
    startup.hStdOutput = stdout_handle
    startup.hStdError = stderr_handle

    process_info = _PROCESS_INFORMATION()
    # WHY 用 create_unicode_buffer：CreateProcessW 允许就地修改命令行缓冲区，
    # 传入不可变的 Python 字符串有触发访问冲突的风险。
    cmdline = ctypes.create_unicode_buffer(_build_command_line(command, env))
    flags = (
        CREATE_SUSPENDED
        | CREATE_UNICODE_ENVIRONMENT
        | CREATE_NO_WINDOW
        | CREATE_BREAKAWAY_FROM_JOB
    )

    if not _kernel32.CreateProcessW(
        None,
        cmdline,
        None,
        None,
        True,  # 继承句柄：子进程才能拿到我们重定向的输出文件
        flags,
        _build_env_block(env),
        str(cwd),
        ctypes.byref(startup),
        ctypes.byref(process_info),
    ):
        code = ctypes.get_last_error()  # type: ignore[attr-defined]
        if code == ERROR_ACCESS_DENIED:
            # WHY 重试：父进程已在某个 Job 中且不允许脱离时，BREAKAWAY 会被
            # 拒绝；此时退回到「继承父 Job」仍可获得超时终止能力，优于直接失败。
            logger.debug("CREATE_BREAKAWAY_FROM_JOB 被拒绝，改用默认方式创建进程")
            if _kernel32.CreateProcessW(
                None,
                cmdline,
                None,
                None,
                True,
                flags & ~CREATE_BREAKAWAY_FROM_JOB,
                _build_env_block(env),
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(process_info),
            ):
                return process_info.hProcess, process_info.hThread
            code = ctypes.get_last_error()  # type: ignore[attr-defined]
        raise ctypes.WinError(code)  # type: ignore[attr-defined]

    return process_info.hProcess, process_info.hThread


def _build_command_line(command: str, env: Mapping[str, str]) -> str:
    """拼接经 shell 执行的命令行。

    WHY 只加 ``/d`` 不加外层引号：``/d`` 跳过注册表中的 AutoRun，避免宿主
    配置悄悄注入命令；而不额外包裹引号是为了与 ``subprocess(shell=True)``
    的行为保持一致——``local`` 档位正是这么跑的，命令解析语义一旦出现差异，
    同一条命令在两个档位下表现不同，排查成本极高。
    """
    comspec = env.get("COMSPEC") or "cmd.exe"
    return f'"{comspec}" /d /c {command}'


def _build_env_block(env: Mapping[str, str]) -> ctypes.c_void_p:
    """构造 Windows 要求的 UTF-16LE 环境块（``K=V\\0...\\0``）。"""
    items = "".join(f"{key}={value}\0" for key, value in env.items())
    raw = (items + "\0").encode("utf-16-le")
    buffer = ctypes.create_string_buffer(raw, len(raw))
    return ctypes.cast(buffer, ctypes.c_void_p)


def _wait_for_exit(process: wintypes.HANDLE, job: wintypes.HANDLE, timeout: int) -> bool:
    """等待进程结束；超时则终止整个 Job。

    Returns:
        ``True`` 表示发生了超时。
    """
    wait_ms = min(int(timeout * 1000), MAX_WAIT_MS)
    result = _kernel32.WaitForSingleObject(process, wait_ms)
    if result == WAIT_OBJECT_0:
        return False
    if result == WAIT_TIMEOUT:
        logger.warning("命令执行超时（%ss），终止整个进程树", timeout)
        _kernel32.TerminateJobObject(job, 1)
        # WHY 终止后仍要等一小段：让句柄状态落到「已退出」，避免读到 STILL_ACTIVE。
        _kernel32.WaitForSingleObject(process, 1000)
        return True
    if result == WAIT_FAILED:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    msg = f"WaitForSingleObject 返回未知状态：{result}"
    raise RuntimeError(msg)


def _resolve_exit_code(process: wintypes.HANDLE, timed_out: bool) -> int | None:
    """读取退出码；超时固定返回 124。"""
    if timed_out:
        return EXIT_CODE_TIMEOUT
    code = wintypes.DWORD()
    if not _kernel32.GetExitCodeProcess(process, ctypes.byref(code)):
        logger.warning("读取退出码失败：%s", ctypes.WinError(ctypes.get_last_error()))  # type: ignore[attr-defined]
        return None
    return int(code.value)


def _read_text(path: str) -> str:
    """读取输出文件并按 UTF-8 容错解码。"""
    with open(path, "rb") as handle:  # noqa: PTH123
        return handle.read().decode("utf-8", errors="replace")
