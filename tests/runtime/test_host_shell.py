"""宿主机命令执行器的回归测试（local 档位与 Tier 0 的 POSIX 分支共用）。

WHY 需要真跑进程：本模块存在的全部理由就是「超时/中止时整棵进程树要消失」，
而这类性质无法用替身验证——替身只会按我们以为的方式返回。
"""

from __future__ import annotations

import sys
import time

import pytest

from runtime.host_shell import HostShellExecutor

_HANG_SECONDS = 30


def _python(code: str) -> str:
    """构造一条用当前解释器执行 ``code`` 的命令。

    WHY 用绝对路径而不是 ``python``：local 档位刻意不继承宿主机环境变量，
    PATH 为空；写 ``python`` 会因为找不到解释器而测不到超时逻辑本身。
    """
    return f'"{sys.executable}" -c "{code}"'


# ------------------------------------------------------------------ 参数校验


def test_constructor_and_run_validation(tmp_path):
    executor = HostShellExecutor()

    with pytest.raises(ValueError):
        executor.run("", cwd=tmp_path, env={}, timeout=5)
    with pytest.raises(ValueError):
        executor.run("echo hi", cwd=str(tmp_path), env={}, timeout=5)
    with pytest.raises(ValueError):
        executor.run("echo hi", cwd=tmp_path / "不存在", env={}, timeout=5)
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            executor.run("echo hi", cwd=tmp_path, env={}, timeout=bad)


# ------------------------------------------------------------------ 正常执行


def test_run_returns_stdout_and_exit_code(tmp_path):
    result = HostShellExecutor().run(
        _python("import sys; sys.stdout.write('hello-out')"),
        cwd=tmp_path,
        env={},
        timeout=15,
    )

    assert result.stdout.strip() == "hello-out"
    assert result.exit_code == 0
    assert result.timed_out is False


def test_run_keeps_stderr_separate(tmp_path):
    result = HostShellExecutor().run(
        _python("import sys; sys.stdout.write('out'); sys.stderr.write('bad')"),
        cwd=tmp_path,
        env={},
        timeout=15,
    )

    assert result.stdout == "out"
    assert result.stderr == "bad"


def test_run_reports_non_zero_exit_code(tmp_path):
    result = HostShellExecutor().run(
        _python("import sys; sys.stderr.write('boom'); sys.exit(3)"),
        cwd=tmp_path,
        env={},
        timeout=15,
    )

    assert result.exit_code == 3
    assert "boom" in result.stderr


def test_run_uses_given_cwd(tmp_path):
    result = HostShellExecutor().run(
        _python("import os,sys; sys.stdout.write(os.getcwd())"),
        cwd=tmp_path,
        env={},
        timeout=15,
    )

    # WHY 比小写：Windows 盘符大小写与 tmp_path 可能不一致。
    assert result.stdout.strip().lower() == str(tmp_path).lower()


def test_run_passes_env_to_child(tmp_path):
    result = HostShellExecutor().run(
        _python("import os,sys; sys.stdout.write(os.environ.get('HARNESS_PROBE',''))"),
        cwd=tmp_path,
        env={"HARNESS_PROBE": "probe-value"},
        timeout=15,
    )

    assert result.stdout.strip() == "probe-value"


# ------------------------------------------------------------------ 超时


def test_timeout_returns_promptly_with_partial_output(tmp_path):
    """WHY 同时断言「返回快」与「带残输出」：这正是原来 ``subprocess.run``
    做不到的两件事——它在杀掉 cmd.exe 后仍会阻塞到孙进程关闭管道写端，
    且超时时丢弃全部已产生的输出。"""
    started = time.monotonic()
    result = HostShellExecutor().run(
        _python(
            "import sys,time; sys.stdout.write('before'); sys.stdout.flush(); "
            f"time.sleep({_HANG_SECONDS})"
        ),
        cwd=tmp_path,
        env={},
        timeout=2,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.exit_code == 124
    # 命令自身寿命 30 秒；若这里等到它自然结束，说明超时根本没生效。
    assert elapsed < 20
    assert "before" in result.stdout


# ------------------------------------------------------------------ 进程树


@pytest.mark.skipif(sys.platform != "win32", reason="进程树回收的实现差异在 Windows 上最致命")
def test_timeout_kills_grandchild_process(tmp_path):
    """WHY 单独验证孙进程：超时只杀直接子进程是本模块要修的原始缺陷——
    孙进程活下来并持有管道写端，父进程就永远读不到 EOF，``execute`` 一直挂着。"""
    from ctypes import WinDLL, wintypes

    pid_file = tmp_path / "grandchild.pid"
    child_script = tmp_path / "grandchild.py"
    child_script.write_text(
        "import time, pathlib\n"
        f"pathlib.Path(r'{pid_file}').write_text(str(__import__('os').getpid()))\n"
        f"time.sleep({_HANG_SECONDS})\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    result = HostShellExecutor().run(
        _python(
            "import subprocess,sys,time;"
            f"subprocess.Popen([sys.executable, r'{child_script}'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"time.sleep({_HANG_SECONDS})"
        ),
        cwd=tmp_path,
        env={},
        timeout=2,
    )

    assert result.timed_out is True
    assert time.monotonic() - started < 20
    assert pid_file.exists(), "孙进程没有真正启动，本用例的前提不成立"

    # 给系统一点时间回收进程表项，再确认孙进程确实不在了。
    time.sleep(1.0)
    kernel32 = WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, int(pid_file.read_text()))  # QUERY_LIMITED
    if handle:
        kernel32.CloseHandle(handle)
    assert not handle, "孙进程在超时后仍然存活——进程树回收失效"
