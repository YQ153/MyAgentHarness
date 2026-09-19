"""沙箱档位装配：把「配置的档位」解析成「可用的 runner」。

两条硬规则贯穿本模块：

1. **降级必须显式**：档位回落要在日志里写明原因与残余风险，绝不静默——
   否则用户会以为命令跑在更强的隔离里。
2. **不可用的档位要报错，不要就近找一个能跑的**：用户显式选择 ``wsl`` 却
   默默用进程沙箱执行，等同于伪造安全边界。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config import SandboxTier
from runtime.sandbox.docker_runner import DockerSandboxRunner
from runtime.sandbox.errors import SandboxUnavailableError
from runtime.sandbox.models import SandboxPolicy
from runtime.sandbox.process_runner import ProcessSandboxRunner
from runtime.sandbox.protocol import SandboxRunner
from runtime.sandbox.wsl_runner import WslSandboxRunner

if TYPE_CHECKING:
    from config import AppConfig

    from runtime.sandbox.models import SandboxPolicy

logger = logging.getLogger(__name__)

_IMPLEMENTED_TIERS: tuple[SandboxTier, ...] = (
    SandboxTier.DOCKER,
    SandboxTier.WSL,
    SandboxTier.PROCESS,
)
"""本期已实现的档位；其余档位按「未实现」显式报错。"""

_AUTO_ORDER: tuple[SandboxTier, ...] = (SandboxTier.WSL, SandboxTier.PROCESS)
"""``auto`` 档位的候选顺序：隔离强度从高到低。

**``docker`` 刻意不进这个列表**（与上面的 ``_IMPLEMENTED_TIERS`` 不同，那是两回事）：
``auto`` 的既有候选之间，「命令能碰到什么」的差异是渐进的；而容器档位一次性改变三件事
——网络被切断、宿主文件系统不可见、shell 从宿主方言变成 POSIX ``sh``。把它们带进 ``auto``
会让升级本版本的用户在毫无预期的情况下遇到「我的构建命令突然连不上网」，而这属于**部署
决定**，应当由使用者显式写下 ``SANDBOX_TIER=docker``。

代价要说清楚：``auto`` 因此在装有 Docker 的机器上可能选到比实际可用的更弱的档位。这与
「auto 一旦静默降级，用户会以为命令跑在更强的隔离里」是同一类问题，只是方向相反——
故它连同本条说明一起写进 README 的档位对照表，而不是留在这里自证清白。
"""


def resolve_tier(requested: SandboxTier) -> tuple[SandboxTier, str]:
    """解析最终生效的档位。

    Args:
        requested: 配置指定的档位。

    Returns:
        ``(生效档位, 选择原因)``，原因用于日志与降级提示。

    Raises:
        SandboxUnavailableError: 请求的档位尚未实现。
    """
    if requested is None:
        msg = "requested 不能为 None"
        raise ValueError(msg)

    if requested == SandboxTier.AUTO:
        return (
            SandboxTier.AUTO,
            f"auto：按 {[item.value for item in _AUTO_ORDER]} 顺序选择首个通过能力探测的档位",
        )

    if requested not in _IMPLEMENTED_TIERS:
        available = [item.value for item in _IMPLEMENTED_TIERS]
        msg = (
            f"沙箱档位 {requested.value!r} 尚未实现；当前可用：{available}。"
            f"请改为 {'/'.join(available)}/auto 之一，不要依赖自动降级到更弱的隔离档位。"
        )
        raise SandboxUnavailableError(msg)

    return requested, "显式指定"


def build_sandbox_runner(config: AppConfig) -> SandboxRunner:
    """按配置装配沙箱 runner。

    Args:
        config: 应用配置，提供档位与资源策略参数。

    Returns:
        已通过可用性探测的 runner。

    Raises:
        ValueError: ``config`` 为 ``None``。
        SandboxUnavailableError: 档位未实现或环境不支持。
    """
    if config is None:
        msg = "config 不能为 None"
        raise ValueError(msg)

    policy = SandboxPolicy.from_config(config)
    tier, reason = resolve_tier(config.sandbox_tier)

    if tier is SandboxTier.AUTO:
        runner, tier, reason = _select_auto_runner(policy, config)
    else:
        runner = _create_runner(tier, policy, config)
        if not runner.probe():
            msg = f"沙箱档位 {tier.value!r} 在当前环境不可用（能力探测未通过）"
            logger.error(msg)
            raise SandboxUnavailableError(msg)

    logger.info("沙箱已就绪：tier=%s（%s）%s", tier.value, reason, runner.describe())
    return runner


def _select_auto_runner(
    policy: SandboxPolicy,
    config: AppConfig,
) -> tuple[SandboxRunner, SandboxTier, str]:
    """按隔离强度从高到低选出首个通过探测的档位。

    WHY 顺序按「强 → 弱」而非「快 → 慢」：``auto`` 的语义是「给我当前能
    拿到的最强隔离」。WSL 档位冷启动与 ``/mnt`` 文件访问都更慢，想要速度
    应当显式选 ``process``，而不是让 auto 悄悄挑一个快的弱档位。

    WHY 每个落选候选都要告警：auto 一旦静默降级，用户会误以为命令跑在更强
    的隔离里——这正是本模块开头写下的第一条硬规则。
    """
    skipped: list[str] = []
    for candidate in _AUTO_ORDER:
        runner = _create_runner(candidate, policy, config)
        if runner.probe():
            reason = f"auto：{candidate.value} 通过能力探测，作为最强可用档位"
            if skipped:
                reason += f"（已跳过：{skipped}）"
            return runner, candidate, reason
        skipped.append(candidate.value)
        logger.warning("auto 档位候选 %s 能力探测未通过，尝试更弱的档位", candidate.value)

    msg = f"没有任何可用的沙箱档位（已尝试：{skipped}）"
    logger.error(msg)
    raise SandboxUnavailableError(msg)


def _create_runner(tier: SandboxTier, policy: SandboxPolicy, config: AppConfig) -> SandboxRunner:
    """按档位创建 runner 实例。"""
    if tier == SandboxTier.PROCESS:
        return ProcessSandboxRunner(policy)
    if tier == SandboxTier.WSL:
        return WslSandboxRunner(policy, distro=config.sandbox_wsl_distro)
    if tier == SandboxTier.DOCKER:
        # WHY 挂载根取 ``config.workspace`` 而不是当前工作目录：runner 只挂载这一个
        # 目录，而「工作区在哪」在本项目里只有配置一处定义（文件后端、文件面板、
        # 附件落盘都按它解析）。此处另取一份会让容器内看到的路径与其余功能不一致。
        return DockerSandboxRunner(
            policy,
            image=config.sandbox_docker_image,
            workspace_root=config.workspace,
            workspace_read_only=config.sandbox_docker_workspace_read_only,
            user=config.sandbox_docker_user,
        )

    msg = f"沙箱档位 {tier.value!r} 没有对应的 runner 实现"
    raise SandboxUnavailableError(msg)
