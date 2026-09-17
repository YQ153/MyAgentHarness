"""用量统计服务。

职责边界：只回答「谁在哪个范围内用了多少 token」，不参与运行推进，也不
知道用量是怎么从模型响应里算出来的（那是 ``application.usage`` 的事）。

WHY 独立于 ``HealthService``：健康检查是「进程此刻能不能干活」，用量是
「过去一段时间干了什么」，两者的时间语义与数据源都不同，合在一起只会让
一个读路径背两份契约。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from application.dto import UsageGroup, UsageSummary
from application.errors import NotFoundError, OwnershipError
from application.ownership import effective_owner_id, ensure_thread_access
from runtime.usage_store import window_start

if TYPE_CHECKING:
    from application.principal import Principal
    from config import AppConfig
    from runtime.thread_store import ThreadMetaStore
    from runtime.usage_store import UsageStore

logger = logging.getLogger(__name__)

_VALID_GROUP_BY = ("model", "thread", "day")
"""允许的聚合维度；与 ``runtime.usage_store`` 的白名单保持一致。"""


class UsageService:
    """按用户 / 会话 / 时间窗聚合 token 用量。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        usage_store: UsageStore,
        thread_store: ThreadMetaStore,
    ) -> None:
        """构造用量服务。

        Args:
            config: 应用配置，提供默认与最大时间窗。
            usage_store: 用量存储。
            thread_store: 会话元数据存储，用于校验「能否看这个会话的用量」。

        Raises:
            ValueError: 任一依赖为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if usage_store is None:
            raise ValueError("usage_store 不能为 None")
        if thread_store is None:
            raise ValueError("thread_store 不能为 None")

        self._config = config
        self._usage_store = usage_store
        self._thread_store = thread_store

        logger.info(
            "用量服务就绪：默认窗口 %d 天，上限 %d 天",
            config.usage_default_window_days,
            config.usage_max_window_days,
        )

    async def summarize(
        self,
        principal: Principal | None = None,
        *,
        thread_id: str | None = None,
        days: int | None = None,
        group_by: str = "model",
    ) -> UsageSummary:
        """汇总指定范围内的用量。

        Args:
            principal: 当前主体；``None`` 仅在认证关闭时使用。
            thread_id: 只统计该会话；``None`` 表示不限会话。
            days: 统计窗口天数；``None`` 表示取配置默认值。
            group_by: 聚合维度，``model`` / ``thread`` / ``day``。

        Returns:
            窗口内的用量汇总与分组明细。

        Raises:
            ValueError: ``days`` 或 ``group_by`` 非法（调用方应映射为 400）。
            NotFoundError: ``thread_id`` 指向的会话不存在。
            OwnershipError: 无权查看该会话的用量。
            RuntimeError: 查询失败（调用方应映射为 500）。
        """
        window_days = self._resolve_days(days)
        if group_by not in _VALID_GROUP_BY:
            raise ValueError(f"group_by 必须是 {list(_VALID_GROUP_BY)} 之一，实际：{group_by}")

        normalized_thread: str | None = None
        if thread_id is not None:
            normalized_thread = await self._ensure_thread_access(thread_id, principal)

        owner_id = self._owner_filter(principal)
        since = window_start(window_days)

        try:
            result = await self._usage_store.summarize(
                owner_id=owner_id,
                thread_id=normalized_thread,
                since=since,
                group_by=group_by,
            )
        except (ValueError, NotFoundError, OwnershipError):
            # WHY 让参数错误与归属错误原样透出：路由层靠类型把它们分别映射为
            # 400 / 404 / 403。若在这里包成 RuntimeError，前端就会把「会话不
            # 存在」或「无权查看」当成一次服务故障。
            raise
        except Exception as exc:
            logger.exception(
                "用量聚合失败：thread=%s days=%s group_by=%s",
                normalized_thread,
                window_days,
                group_by,
            )
            raise RuntimeError("用量聚合失败") from exc

        groups = [UsageGroup(**item) for item in result.get("groups") or []]
        summary = UsageSummary(
            window_days=window_days,
            since=since,
            group_by=group_by,
            thread_id=normalized_thread,
            prompt_tokens=int(result.get("prompt_tokens") or 0),
            completion_tokens=int(result.get("completion_tokens") or 0),
            total_tokens=int(result.get("total_tokens") or 0),
            run_count=int(result.get("run_count") or 0),
            groups=groups,
        )
        logger.debug(
            "用量汇总完成：window=%d 天 total=%d runs=%d",
            window_days,
            summary.total_tokens,
            summary.run_count,
        )
        return summary

    # ------------------------------------------------------------------ 内部

    def _resolve_days(self, days: int | None) -> int:
        """校验并解析时间窗天数。"""
        resolved = self._config.usage_default_window_days if days is None else days
        if not isinstance(resolved, int) or isinstance(resolved, bool):
            raise ValueError(f"days 必须是整数，实际：{type(resolved).__name__}")
        if resolved < 1 or resolved > self._config.usage_max_window_days:
            raise ValueError(
                f"days 必须在 1..{self._config.usage_max_window_days} 之间，实际：{resolved}"
            )
        return resolved

    def _owner_filter(self, principal: Principal | None) -> str | None:
        """返回按所有者过滤用的 ``owner_id``；管理员与认证关闭时不过滤。"""
        if self._config.auth_mode == "disabled":
            return None
        if principal is not None and principal.is_admin():
            return None
        return effective_owner_id(self._config, principal)

    async def _ensure_thread_access(
        self,
        thread_id: str,
        principal: Principal | None,
    ) -> str:
        """校验主体能否查看该会话的用量，并返回规范化后的会话 ID。

        WHY 必须回读会话元数据：用量表里既有 ``owner_id`` 也有 ``thread_id``，
        只按 owner 过滤挡不住「猜别人 thread_id 直接查」；以会话的归属为准
        才与会话读权限保持一致。
        """
        if not isinstance(thread_id, str):
            raise ValueError(f"thread_id 必须是字符串，实际：{type(thread_id).__name__}")
        normalized = thread_id.strip()
        if not normalized:
            raise ValueError("thread_id 不能为空")

        try:
            record = await self._thread_store.get(normalized)
        except Exception as exc:
            logger.exception("读取会话元数据失败：thread=%s", normalized)
            raise RuntimeError("读取会话元数据失败") from exc

        ensure_thread_access(record, normalized, self._config, principal)
        return normalized

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志排错
        return f"UsageService(default_days={self._config.usage_default_window_days})"


__all__ = ["UsageService"]
