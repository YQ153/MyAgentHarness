"""认证主体与权限模型。

职责边界：只描述「已认证主体是谁、能做什么」，不涉及任何 HTTP 或存储细节。
WHY 独立成模块：Principal 是应用层多个服务共享的概念，若放在 web 层会让
ThreadService/RunService 反向依赖接口层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

# WHY 从 agent 层取常量：匿名主体标识必须与长期记忆命名空间用的兜底标识是
# 同一个值，否则「认证关闭」与「未带运行时」两种兜底会落进两个记忆池。
# 方向只能是 agent → application 的反向（agent 不得依赖本包），因此常量定义
# 放在 agent.run_context，这里引用它而不是各写一份字面量。
from agent.run_context import ANONYMOUS_USER_ID


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
    "thread:update": "PATCH /api/threads/{id}（重命名 / 归档）",
    "thread:delete": "DELETE /api/threads/{id}",
    "hitl:approve": "POST /api/threads/{id}/resume（人工审批决策）",
    "file:read": "FilesystemBackend read",
    "file:write": "FilesystemBackend write",
    "file:execute": "execute tool in sandbox/local mode",
    "system:models": "GET /api/models",
    "usage:read": "GET /api/usage（用量统计）",
    "tool:read": "GET /api/tools（生效工具清单与 MCP 服务器状态）",
    "memory:read": "GET /api/memories（长期记忆清单）",
    "memory:delete": "DELETE /api/memories/{path}（删除单条长期记忆）",
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
    # WHY member 也有 usage:read：用量是「自己的成本」，与「读自己的会话」
    # 同量级；不给的话成员连自己花了多少 token 都看不到，只能去翻审计。
    # 服务层仍按 owner_id 收敛，拿到权限也读不到别人的数据。
    "member": frozenset(
        {
            "thread:read",
            "thread:list",
            "thread:create",
            # WHY member 也有 thread:update：给会话改名与归档都是「整理自己的
            # 清单」，与删除同属所有者对自己数据的处置权；归档可逆，风险更低。
            "thread:update",
            "hitl:approve",
            "file:read",
            "usage:read",
            # WHY member 也有 tool:read：工具清单回答的是「这个助手能做什么」，
            # 是使用者的基本知情项；它不含任何他人数据，收紧到 admin 只会
            # 让成员靠猜。真正敏感的是「调用工具」，而那由执行审批把关。
            "tool:read",
            # WHY member 也有 memory:delete：记忆是 Agent 对「我」的画像，
            # 「让它忘掉这条」属于使用者自查自纠的一部分，与删除自己的会话
            # 同量级；服务层按 owner 收敛，拿到权限也删不到别人的记忆。
            "memory:read",
            "memory:delete",
        }
    ),
    "admin": frozenset(PERMISSIONS.keys()),
}

# 系统保留的匿名主体，用于 auth_mode=disabled 保持向后兼容。
ANONYMOUS_PRINCIPAL = Principal(user_id=ANONYMOUS_USER_ID, role="admin", auth_method="disabled")
