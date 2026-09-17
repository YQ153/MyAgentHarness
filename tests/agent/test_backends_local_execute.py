"""local 档位 backend 的 execute 回归测试。

WHY 覆盖这一层：``HostShellExecutor`` 自己测过了，但 backend 是模型看到的
那一层——输出格式（``[stderr]`` 前缀、``<no output>``、超时文案）一旦变了，
模型的行为会跟着变，而执行器测试发现不了。
"""

from __future__ import annotations

import sys

import pytest

from agent.backends import _ExtendedPathSafeLocalShellBackend

_HANG_SECONDS = 30


def _python(code: str) -> str:
    """本地档位不继承 PATH，命令里必须用解释器的绝对路径。"""
    return f'"{sys.executable}" -c "{code}"'


@pytest.fixture
def backend(tmp_path) -> _ExtendedPathSafeLocalShellBackend:
    return _ExtendedPathSafeLocalShellBackend(
        root_dir=str(tmp_path),
        virtual_mode=True,
        timeout=15,
        max_output_bytes=100_000,
        # WHY 与生产一致地不继承环境变量：本用例要证明的正是「空环境下
        # 命令依然能起来并按时返回」。
        inherit_env=False,
    )


def test_execute_rejects_empty_command(backend):
    response = backend.execute("")

    assert response.exit_code == 1
    assert "non-empty" in response.output


def test_execute_returns_stdout(backend):
    response = backend.execute(_python("import sys; sys.stdout.write('hello')"))

    assert response.output == "hello"
    assert response.exit_code == 0
    assert response.truncated is False


def test_execute_prefixes_stderr(backend):
    """WHY 断言 ``[stderr]`` 前缀：这是模型学会的约定，换执行器不能改。"""
    response = backend.execute(
        _python("import sys; sys.stdout.write('out'); sys.stderr.write('bad')")
    )

    assert "[stderr] bad" in response.output
    assert "out" in response.output


def test_execute_reports_exit_code(backend):
    response = backend.execute(_python("import sys; sys.stderr.write('boom'); sys.exit(3)"))

    assert response.exit_code == 3
    assert "Exit code: 3" in response.output
    assert "[stderr] boom" in response.output


def test_execute_uses_no_output_placeholder(backend):
    response = backend.execute(_python("pass"))

    assert response.output == "<no output>"
    assert response.exit_code == 0


def test_execute_truncates_long_output(backend):
    backend._max_output_bytes = 50
    response = backend.execute(_python("import sys; sys.stdout.write('x' * 500)"))

    assert "Output truncated" in response.output
    assert response.truncated is True


def test_execute_reports_timeout_with_partial_output(backend):
    """WHY 断言超时仍带输出：上游 ``subprocess.run`` 超时时丢弃全部输出，
    而恰恰是「死前现场」告诉模型该改什么。"""
    response = backend.execute(
        _python(
            "import sys,time; sys.stdout.write('before'); sys.stdout.flush(); "
            f"time.sleep({_HANG_SECONDS})"
        ),
        timeout=2,
    )

    assert "timed out" in response.output
    assert "before" in response.output
    assert response.exit_code == 124


def test_execute_rejects_non_positive_timeout(backend):
    with pytest.raises(ValueError):
        backend.execute(_python("pass"), timeout=0)
