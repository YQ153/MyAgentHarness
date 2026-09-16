"""``SandboxRunner`` 协议：所有沙箱档位统一的执行接口。

WHY 用 Protocol 而非 ABC：runner 的实现分属不同平台（Windows Job Object、
WSL、容器），它们之间没有可复用的共同基类；用结构化类型约束可以让新增档位
只关心自己的实现，不必继承一堆无意义的空方法。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from config import SandboxTier

    from runtime.sandbox.models import CommandRequest, CommandResult


@runtime_checkable
class SandboxRunner(Protocol):
    """命令执行沙箱。

    契约要求：

    - ``run`` 永不抛出与命令本身相关的异常——命令失败应体现为 ``CommandResult``
      的退出码；只有「沙箱自身故障」才抛异常，由调用方转为用户可见的错误。
    - ``run`` 必须保证超时后不残留进程（这是沙箱区别于裸 ``subprocess`` 的核心）。
    - ``probe`` 与 ``describe`` 必须廉价且无副作用，供启动期选档与日志展示使用。
    """

    @property
    def tier(self) -> SandboxTier:
        """本 runner 对应的沙箱档位。"""
        ...

    def probe(self) -> bool:
        """探测当前环境是否真的可用（带超时，失败返回 ``False``）。"""
        ...

    def describe(self) -> str:
        """返回一行人类可读的档位说明，用于启动日志与降级提示。"""
        ...

    def run(self, request: CommandRequest) -> CommandResult:
        """在沙箱内执行一条命令。

        Args:
            request: 命令请求。

        Returns:
            执行结果。

        Raises:
            SandboxPolicyError: 请求违反策略。
            SandboxError: 沙箱自身故障（例如无法创建隔离环境）。
        """
        ...

    def close(self) -> None:
        """释放 runner 持有的资源；多次调用必须安全。"""
        ...
