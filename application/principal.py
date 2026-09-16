"""认证主体与权限模型。

职责边界：只描述「已认证主体是谁、能做什么」，不涉及任何 HTTP 或存储细节。
WHY 独立成模块：Principal 是应用层多个服务共享的概念，若放在 web 层会让
ThreadService/RunService 反向依赖接口层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet


@dataclass(frozen=True)
class Principal:
    """已认证主体的不可变描述。

    Args:
        user_id: 系统内稳定用户标识；OIDC 场景下取 ``sub``，API Key 场景下取 key 前缀。
        display_name: 展示名称，可选。
        email: 邮箱，可选。
        role: 角色，决定权限集合。
        scopes: OAuth scope 集合，用于委派场景下的粗粒度校验。
        auth_method: 认证来源，如 ``cookie``、``apikey``、``oidc``。
    """

    user_id: str
    display_name: str = ""
    email: str = ""
    role: str = "member"
    scopes: FrozenSet[str] = field(default_factory=frozenset)
    auth_method: str = ""

    def has_permission(self, permission: str) -> bool:
        """主体是否拥有某权限。"""
        if not isinstance(permission, str) or not permission:
            return False
        perms = ROLE_PERMISSIONS.get(self.role)
        if perms is None:
            return False
        if "admin:all" in perms:
            return True
        return permission in perms

    def has_scope(self, scope: str) -> bool:
        """主体是否持有某 OAuth scope。"""
        return scope in self.scopes

    def is_admin(self) -> bool:
        """是否为管理员。"""
        return self.role == "admin"


# 权限到端点/操作的映射（文档化用途）。
PERMISSIONS = {
    "thread:list": "GET /api/threads",
    "thread:read": "GET /api/threads/{id}",
    "thread:create": "POST /api/threads/{id}/runs",
    "thread:delete": "DELETE /api/threads/{id}",
    "hitl:approve": "POST /api/threads/{id}/resume（人工审批决策）",
    "file:read": "FilesystemBackend read",
    "file:write": "FilesystemBackend write",
    "file:execute": "execute tool in sandbox/local mode",
    "system:models": "GET /api/models",
    "apikey:manage": "管理 API Key（创建/列出/吊销）",
    "audit:read": "读取审计日志",
    "admin:all": "所有资源的所有操作",
}

# 角色到权限集合的映射。默认成员只读自己的会话、发送消息、审批自己的中断。
#
# WHY ``hitl:approve`` 独立于 ``thread:create``：审批是「授权高危工具真正
# 执行」的动作，与「发起一轮对话」的风险量级不同。合成一个权限后，
# 只读角色（viewer）只要能发消息就能批准任意 shell 执行——这正是 HITL
# 作为主防线要挡住的路径。
ROLE_PERMISSIONS: dict[str, FrozenSet[str]] = {
    "viewer": frozenset({"thread:read", "thread:list"}),
    "member": frozenset(
        {"thread:read", "thread:list", "thread:create", "hitl:approve", "file:read"}
    ),
    "admin": frozenset(PERMISSIONS.keys()),
}

# 系统保留的匿名主体，用于 auth_mode=disabled 保持向后兼容。
ANONYMOUS_PRINCIPAL = Principal(user_id="__anonymous__", role="admin", auth_method="disabled")
