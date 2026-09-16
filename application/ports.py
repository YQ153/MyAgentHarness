"""应用层对外暴露的基础设施端口。

WHY 需要端口：``interfaces`` 层需要操作审计日志、API Key 与 device flow 状态，
但不应依赖 ``runtime`` 的具体实现类——那会让「更换存储实现」波及接口层，
也破坏 ``interfaces → application`` 的单向依赖。

用 ``typing.Protocol`` 描述所需能力后：

- ``interfaces`` 只依赖 ``application``，依赖方向合规；
- ``runtime`` 的实现类因结构化子类型（structural subtyping）自动满足协议，
  无需显式继承，也无需反向导入本模块，因此不产生新的耦合。
"""

from __future__ import annotations

from typing import Any, Protocol


class AuditSink(Protocol):
    """审计事件的写入与查询能力。"""

    async def log(
        self,
        *,
        event_type: str,
        actor_id: str,
        target_id: str | None = None,
        action: str | None = None,
        outcome: str,
        ip: str | None = None,
        user_agent: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """记录一条审计事件。"""
        ...

    async def list(
        self,
        *,
        actor_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按时间倒序列出审计事件。"""
        ...


class APIKeyRepository(Protocol):
    """API Key 的创建、校验、列举与吊销能力。"""

    async def create(
        self,
        *,
        role: str = "member",
        scopes: list[str] | None = None,
        description: str = "",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """创建一条 API Key。

        Returns:
            含 ``key_id`` 与一次性明文 ``key`` 的字典。
        """
        ...

    async def validate(self, key: str) -> dict[str, Any] | None:
        """校验 API Key；有效时返回记录，否则返回 ``None``。"""
        ...

    async def list(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        """列出 API Key。"""
        ...

    async def revoke(self, key_id: str) -> bool:
        """吊销指定 API Key，返回是否实际发生变更。"""
        ...


class DeviceFlowRepository(Protocol):
    """CLI 设备授权流程的状态存储能力。"""

    async def create(self, *, expires_in_seconds: int = 600) -> dict[str, Any]:
        """创建一条 pending 记录，返回 device_code 与 user_code 等字段。"""
        ...

    async def get_by_device_code(self, device_code: str) -> dict[str, Any] | None:
        """按 device_code 查询记录。"""
        ...

    async def get_by_user_code(self, user_code: str) -> dict[str, Any] | None:
        """按 user_code 查询记录。"""
        ...

    async def approve(
        self,
        user_code: str,
        *,
        principal: dict[str, Any],
        api_key: str,
    ) -> bool:
        """批准指定 user_code，返回是否成功。"""
        ...

    async def cleanup(self, max_age_seconds: int = 86400) -> int:
        """清理过期记录，返回清理数量。"""
        ...


class RateLimiterPort(Protocol):
    """按 key 的请求限流能力。"""

    def is_allowed(self, key: str) -> bool:
        """判断某 key 在当前窗口内是否仍可发起请求。"""
        ...
