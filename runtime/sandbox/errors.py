"""沙箱执行层的异常类型。

WHY 单独定义异常族：``runner.run()`` 的调用方（backend）需要区分「沙箱不可用」
（配置/环境问题，应显式报错引导用户）与「命令执行失败」（命令自身问题，应把
输出交给模型），二者混用一种异常会让上层无法给出正确处置。
"""

from __future__ import annotations


class SandboxError(RuntimeError):
    """沙箱执行层的基类异常。"""


class SandboxUnavailableError(SandboxError):
    """请求的沙箱档位在当前环境不可用。

    抛出意味着**不应静默降级**——用户选择了一个隔离档位就必须得到它，
    否则会出现「以为在沙箱里、实际在裸跑」的安全错觉。
    """


class SandboxPolicyError(SandboxError):
    """请求违反了沙箱策略（命令为空、工作目录不存在、参数非法等）。"""


class SandboxTimeoutError(SandboxError):
    """命令执行超过策略允许的时间。"""
