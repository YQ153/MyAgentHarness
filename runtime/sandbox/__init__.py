"""沙箱执行层：把「命令在哪跑、能碰多少资源」收敛到一处。

对外只暴露三样东西：

- ``SandboxRunner``：所有档位统一的执行协议；
- ``build_sandbox_runner``：按档位装配 runner（含能力探测与降级日志）；
- ``SandboxError`` 家族：沙箱自身故障的异常类型。

文件布局：

- ``models.py``    跨档位通用的数据结构；
- ``protocol.py``  ``SandboxRunner`` 协议；
- ``_winjob.py``   Windows Job Object + ``CreateProcessW`` 的 ctypes 封装；
- ``process_runner.py``  Tier 0 进程沙箱（Windows 走 Job，POSIX 走进程组）；
- ``wsl_runner.py``  Tier 1 WSL 发行版沙箱（Linux rlimit + GNU timeout）；
- ``factory.py``   档位装配与探测。
"""

from __future__ import annotations

from runtime.sandbox.errors import (
    SandboxError,
    SandboxPolicyError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from runtime.sandbox.factory import (
    build_sandbox_runner,
    resolve_tier,
)
from runtime.sandbox.models import (
    CommandRequest,
    CommandResult,
    SandboxPolicy,
)
from runtime.sandbox.process_runner import ProcessSandboxRunner
from runtime.sandbox.protocol import SandboxRunner
from runtime.sandbox.wsl_runner import WslSandboxRunner

__all__ = [
    "CommandRequest",
    "CommandResult",
    "ProcessSandboxRunner",
    "WslSandboxRunner",
    "SandboxError",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxRunner",
    "SandboxTimeoutError",
    "SandboxUnavailableError",
    "build_sandbox_runner",
    "resolve_tier",
]
