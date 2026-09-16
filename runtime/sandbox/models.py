"""沙箱执行层的数据模型。

这里只放「跨档位通用」的数据结构：一次命令请求、一次命令结果、以及一份资源
与网络策略。平台相关的结构（Job Object、cgroup）留在各自的 runner 实现里，
避免抽象层被某个平台的概念污染。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from config import DEFAULT_ENV_ALLOWLIST, NETWORK_MODE_HOST, NETWORK_MODE_NONE, SandboxTier

if TYPE_CHECKING:
    from collections.abc import Mapping

    from config import AppConfig

VALID_NETWORK_MODES: tuple[str, ...] = (NETWORK_MODE_NONE, NETWORK_MODE_HOST)

_NETWORK_ENV_MARKERS = ("_PROXY",)
"""网络模式为 ``none`` 时需剔除的环境变量后缀（``HTTP_PROXY`` 等）。"""


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """一次沙箱执行所遵循的资源与隔离策略。

    设计成不可变对象：策略在 runner 生命周期内被多次执行共享，可变会让
    「某次执行偷偷放宽了限制」这类问题难以复现。
    """

    max_processes: int = 64
    """活动进程数上限，用于阻断 fork bomb。"""

    max_memory_mb: int = 2048
    """单个进程的内存上限（MB）。"""

    cpu_percent: int = 50
    """CPU 占用硬上限百分比（1-100）。"""

    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    """允许传入子进程的环境变量名。"""

    network_mode: str = NETWORK_MODE_NONE
    """网络模式；``none`` 表示不向子进程传递任何代理类变量。"""

    def __post_init__(self) -> None:
        """校验策略取值，避免非法值在 Windows API 处才报错。"""
        if self.max_processes < 1:
            msg = f"max_processes 必须 >= 1，当前为 {self.max_processes}"
            raise ValueError(msg)
        if self.max_memory_mb < 64:
            msg = f"max_memory_mb 必须 >= 64，当前为 {self.max_memory_mb}"
            raise ValueError(msg)
        if not 1 <= self.cpu_percent <= 100:  # noqa: PLR2004
            msg = f"cpu_percent 必须在 1-100 之间，当前为 {self.cpu_percent}"
            raise ValueError(msg)
        if self.network_mode not in VALID_NETWORK_MODES:
            msg = f"network_mode 必须是 {VALID_NETWORK_MODES} 之一，当前为 {self.network_mode}"
            raise ValueError(msg)

    def sanitized_env(self, source: Mapping[str, str] | None = None) -> dict[str, str]:
        """按白名单从源环境里挑出可传递的变量。

        Args:
            source: 源环境；``None`` 时使用 ``os.environ``。

        Returns:
            仅含白名单变量的新字典。
        """
        origin = os.environ if source is None else source
        allowed = {name.upper() for name in self.env_allowlist}
        return {key: value for key, value in origin.items() if key.upper() in allowed}

    def child_env(self, source: Mapping[str, str] | None = None) -> dict[str, str]:
        """构造交给子进程的环境：白名单过滤 + 网络模式为 ``none`` 时剔除代理。

        WHY 收敛在策略对象上：Tier 0（宿主进程）与 Tier 1（WSL）需要完全
        一致的清洗口径，各写一份迟早会出现「某一档位漏掉了代理变量」这类
        偏差，而偏差的方向一定是「更松」。
        """
        env = self.sanitized_env(source)
        if self.network_mode != NETWORK_MODE_NONE:
            return env
        return {
            key: value
            for key, value in env.items()
            if not key.upper().endswith(_NETWORK_ENV_MARKERS)
        }

    @classmethod
    def from_config(cls, config: AppConfig) -> SandboxPolicy:
        """从应用配置构造策略。

        WHY 收敛在此：策略字段散落在装配代码里时，新增一个限制项要同时改
        配置、工厂与 runner 三处，漏改就会出现「配了但没生效」。
        """
        if config is None:
            msg = "config 不能为 None"
            raise ValueError(msg)
        return cls(
            max_processes=config.sandbox_max_processes,
            max_memory_mb=config.sandbox_max_memory_mb,
            cpu_percent=config.sandbox_cpu_percent,
            env_allowlist=tuple(config.sandbox_env_allowlist),
            network_mode=config.sandbox_network_mode,
        )


@dataclass(frozen=True, slots=True)
class CommandRequest:
    """一次命令执行的完整输入。"""

    command: str
    """待执行的 shell 命令原文。"""

    cwd: Path
    """工作目录；命令只允许在此目录内活动。"""

    timeout: int
    """超时秒数，必须为正数。"""

    max_output_bytes: int = 100_000
    """stdout 与 stderr 各自的输出截断阈值（字节）。"""

    env: Mapping[str, str] | None = None
    """已清洗的环境变量；``None`` 时由 runner 按策略自行清洗。"""

    def __post_init__(self) -> None:
        """校验请求参数。"""
        if not isinstance(self.command, str) or not self.command.strip():
            msg = "command 必须是非空字符串"
            raise ValueError(msg)
        if self.timeout <= 0:
            msg = f"timeout 必须为正数，当前为 {self.timeout}"
            raise ValueError(msg)
        if self.max_output_bytes <= 0:
            msg = f"max_output_bytes 必须为正数，当前为 {self.max_output_bytes}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """一次命令执行的结果。"""

    stdout: str = ""
    """标准输出（可能已截断）。"""

    stderr: str = ""
    """标准错误（可能已截断）。"""

    exit_code: int | None = None
    """进程退出码；``None`` 表示无法确定。"""

    truncated: bool = False
    """输出是否因超过阈值被截断。"""

    timed_out: bool = False
    """是否因超时被强制终止。"""

    tier: SandboxTier = SandboxTier.PROCESS
    """本次执行实际生效的沙箱档位。"""

    @property
    def succeeded(self) -> bool:
        """命令是否成功结束（未超时且退出码为 0）。"""
        return not self.timed_out and self.exit_code == 0
