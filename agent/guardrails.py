"""安全边界：文件系统权限规则与 HITL 中断配置。

集中在此文件的理由是**散落的护栏等于没有护栏**：一旦某处新增工具却忘了配
规则，就会静默获得越权能力。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from deepagents import FilesystemPermission
from deepagents.middleware.filesystem import supports_execution
from langchain.agents.middleware import InterruptOnConfig

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol

    from config import ExecutionMode, SandboxTier

logger = logging.getLogger(__name__)

_OPERATION_READ = "read"
_OPERATION_WRITE = "write"

_SANDBOX_INTERRUPT_DESCRIPTIONS: dict[str, str] = {
    "process": (
        "即将在进程沙箱（Tier 0）内执行 shell 命令：该档位只做资源与进程树"
        "管控，不是安全边界，请确认命令内容安全。"
    ),
    "wsl": (
        "即将在 WSL 沙箱（Tier 1）内执行 shell 命令：命令跑在 WSL 发行版里，"
        "与宿主之间隔着 utility VM 边界，但发行版是持久环境、且经 /mnt 仍能"
        "读写宿主文件，被写入的恶意脚本会活到下一次执行，请确认命令内容安全。"
    ),
    "docker": "即将在容器（Tier 2）内执行 shell 命令，请确认命令内容安全。",
}
"""各沙箱档位的审批提示语。

WHY 提示语要写清隔离强度：审批的价值取决于人能否做出正确判断，而正确判断
的前提是知道「拒绝这道命令的代价是什么，放行的风险又是什么」。
"""

_DEFAULT_SANDBOX_DESCRIPTION = _SANDBOX_INTERRUPT_DESCRIPTIONS["process"]

_SECRET_PATTERNS: tuple[str, ...] = (
    "/**/.env",
    "/**/.env.*",
    "/**/.git/**",
    "/**/.ssh/**",
    "/**/id_rsa",
    "/**/id_ed25519",
    "/**/*.pem",
    "/**/*.key",
    "/**/*.p12",
)
"""敏感文件路径模式。

WHY 用黑名单而非白名单：虚拟根目录已经把可见范围限制在工作区内，
剩下的真实风险是「工作区内恰好存在凭据文件」，用黑名单精准封住即可。
"""


def build_permissions(backend: BackendProtocol | None = None) -> list[FilesystemPermission]:
    """构造文件系统权限规则。

    规则按声明顺序匹配，首个命中即生效，因此**窄而严的规则必须写在宽而松的
    规则之前**。

    WHY 必须按 backend 能力裁剪：工具级权限只作用于 ``ls`` / ``read_file`` /
    ``write_file`` 这些**工具**，而 ``execute`` 走的是 shell——一条 ``cat .env``
    能绕过全部路径规则。deepagents 因此在「可执行 backend + 权限规则」组合上
    直接抛 ``NotImplementedError``（拒绝假装权限仍然生效），而不是放行一条纸面
    防线。可执行 backend 下只能返回空列表，把凭据防护交给 ``execute`` 的人工
    审批（``build_interrupt_on``）与沙箱隔离本身；该取舍会被显式告警，**不做
    静默降级**。

    Args:
        backend: 已装配的 backend。``None`` 表示调用方自行保证不会执行命令
            （例如纯配置校验），按「不执行命令」处理并返回完整规则。

    Note:
        能力判定看的是 ``backend`` 的**默认后端**——``supports_execution`` 对
        ``CompositeBackend`` 会下钻到 ``default``，与生产装配同形。

    Returns:
        权限规则列表；backend 具备命令执行能力时为空列表。
    """
    rules = [
        # 1. 窄规则：敏感文件一律拒绝
        FilesystemPermission(
            operations=[_OPERATION_READ, _OPERATION_WRITE],
            paths=list(_SECRET_PATTERNS),
            mode="deny",
        ),
        # 2. 读操作整体放行：读不产生副作用，拦截只会拖慢任务
        FilesystemPermission(
            operations=[_OPERATION_READ],
            paths=["/**"],
            mode="allow",
        ),
        # 3. 写操作放行：虚拟根已把范围限制在工作区，无需再逐次人工确认
        FilesystemPermission(
            operations=[_OPERATION_WRITE],
            paths=["/**"],
            mode="allow",
        ),
    ]

    if backend is None or not supports_execution(backend):
        return rules

    logger.warning(
        "backend 具备命令执行能力：已停用工具级文件权限规则（含 %d 条敏感路径拒绝）。"
        "execute 经 shell 执行，无法被路径规则约束，deepagents 拒绝该组合；"
        "凭据防护改由 execute 的人工审批与沙箱隔离承担。",
        len(_SECRET_PATTERNS),
    )
    return []


def build_interrupt_on(
    mode: ExecutionMode,
    tier: SandboxTier | None = None,
    *,
    require_approval: bool = True,
) -> dict[str, bool | InterruptOnConfig]:
    """按「执行档位 + 沙箱档位」二维构造中断配置。

    只有真正能执行命令时才需要审批——``disabled`` 档位下 ``execute`` 工具
    自身会返回错误，再叠加一层审批只是让用户反复确认一个注定失败的调用。

    WHY 审批严格度必须随档位联动：Tier 0 进程沙箱只能挡住失控与资源耗尽，
    挡不住本地提权与凭据嗅探，此时审批才是真正的主防线；等将来接入容器或
    微 VM，审批才可以让位给隔离本身。用一个开关管所有档位，必然在弱档位
    上过松、强档位上过严。

    Args:
        mode: 执行档位。
        tier: 沙箱档位，仅 ``sandbox`` 档位使用；``None`` 按 Tier 0 处理。
        require_approval: 沙箱档位是否仍需人工审批。

    Returns:
        中断配置字典。
    """
    if mode is None:
        raise ValueError("mode 不能为 None")

    if mode.value == "local":
        logger.info("执行档位=local：execute 调用前需人工批准")
        return {
            "execute": InterruptOnConfig(
                allowed_decisions=["approve", "reject"],
                description="即将在本机执行 shell 命令，请确认命令内容安全。",
            )
        }

    if mode.value == "sandbox":
        if not require_approval:
            logger.warning(
                "执行档位=sandbox：已按配置关闭人工审批，命令将直接执行，"
                "仅依赖沙箱自身的资源管控"
            )
            return {}

        tier_value = tier.value if tier is not None else "auto"
        description = _SANDBOX_INTERRUPT_DESCRIPTIONS.get(tier_value, _DEFAULT_SANDBOX_DESCRIPTION)
        logger.info("执行档位=sandbox（tier=%s）：execute 调用前需人工批准", tier_value)
        return {
            "execute": InterruptOnConfig(
                allowed_decisions=["approve", "reject"],
                description=description,
            )
        }

    logger.info("执行档位=%s：execute 不开放审批通道", mode.value)
    return {}
