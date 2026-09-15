"""安全边界：文件系统权限规则与 HITL 中断配置。

集中在此文件的理由是**散落的护栏等于没有护栏**：一旦某处新增工具却忘了配
规则，就会静默获得越权能力。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from deepagents import FilesystemPermission
from langchain.agents.middleware import InterruptOnConfig

if TYPE_CHECKING:
    from config import ExecutionMode

logger = logging.getLogger(__name__)

_OPERATION_READ = "read"
_OPERATION_WRITE = "write"

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


def build_permissions() -> list[FilesystemPermission]:
    """构造文件系统权限规则。

    规则按声明顺序匹配，首个命中即生效，因此**窄而严的规则必须写在宽而松的
    规则之前**。

    注意：这里的路径是虚拟文件系统路径（相对于 backend 的根目录），
    不是宿主机绝对路径。
    """
    return [
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


def build_interrupt_on(mode: ExecutionMode) -> dict[str, bool | InterruptOnConfig]:
    """按执行档位构造中断配置。

    只有真正能执行命令时才需要审批——``disabled`` 档位下 ``execute`` 工具
    自身会返回错误，再叠加一层审批只是让用户反复确认一个注定失败的调用。
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

    logger.info("执行档位=%s：execute 不开放审批通道", mode.value)
    return {}
