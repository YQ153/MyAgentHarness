"""会话归属的统一判定。

WHY 抽成模块：``owner_id`` 过滤值的取法与「这条会话能不能访问」的判定，
此前在运行服务、会话服务里各有一份；用量统计是第三个需要它的地方。三处
各自实现后，任何一条规则调整（例如管理员是否绕过、未登记会话能否认领）
都只会在一处生效，另外两处静默保持旧语义——这正是「权限漂移」的典型路径。

本模块只依赖 ``application.errors``，不触碰存储与 HTTP。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from application.errors import NotFoundError, OwnershipError

if TYPE_CHECKING:
    from application.principal import Principal
    from config import AppConfig

UNAUTHENTICATED_OWNER = "__unauthenticated__"
"""未认证主体在查询时使用的占位 owner_id。

WHY 用一个不可能匹配的常量而不是空串：空串在库里表示「未认证场景下创建的
会话」，用它做过滤会把这些会话全部算成当前主体的，属于越权。
"""


def effective_owner_id(config: AppConfig, principal: Principal | None) -> str | None:
    """返回查询时使用的 ``owner_id`` 过滤值。

    Args:
        config: 应用配置，决定是否启用鉴权。
        principal: 当前主体；``None`` 表示未认证。

    Returns:
        认证关闭时返回 ``None``（不过滤，向后兼容）；未认证时返回
        :data:`UNAUTHENTICATED_OWNER`；否则返回主体的 ``user_id``。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    if config.auth_mode == "disabled":
        return None
    if principal is None:
        return UNAUTHENTICATED_OWNER
    return principal.user_id


def ensure_thread_access(
    record: dict[str, Any] | None,
    thread_id: str,
    config: AppConfig,
    principal: Principal | None,
    *,
    allow_claim: bool = False,
    require_admin: bool = False,
) -> dict[str, Any]:
    """校验主体能否访问该会话，并返回记录。

    Args:
        record: 存储层读出的元数据；``None`` 表示会话不存在。
        thread_id: 已规范化的会话 ID，仅用于错误信息。
        config: 应用配置。
        principal: 当前主体。
        allow_claim: 会话未登记时视为「可被当前主体认领」，返回空字典。
        require_admin: 该操作仅管理员可执行。

    Returns:
        元数据记录；``allow_claim`` 且会话未登记时返回空字典。

    Raises:
        ValueError: ``config`` 为 ``None``。
        NotFoundError: 会话不存在且 ``allow_claim=False``。
        OwnershipError: 无访问权限。
    """
    if config is None:
        raise ValueError("config 不能为 None")

    if record is None:
        if allow_claim:
            return {}
        raise NotFoundError("会话", thread_id)

    if config.auth_mode == "disabled":
        return record
    if principal is None:
        raise OwnershipError("会话", thread_id)
    if principal.is_admin():
        return record

    owner_id = record.get("owner_id") or ""
    if owner_id and owner_id != principal.user_id:
        raise OwnershipError("会话", thread_id)
    if require_admin:
        raise OwnershipError("会话", thread_id)
    return record


__all__ = ["UNAUTHENTICATED_OWNER", "effective_owner_id", "ensure_thread_access"]
