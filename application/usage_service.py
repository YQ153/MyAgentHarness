"""用量统计服务。

职责边界：只回答「谁在哪个范围内用了多少 token」，不参与运行推进，也不
知道用量是怎么从模型响应里算出来的（那是 ``application.usage`` 的事）。

WHY 独立于 ``HealthService``：健康检查是「进程此刻能不能干活」，用量是
「过去一段时间干了什么」，两者的时间语义与数据源都不同，合在一起只会让
一个读路径背两份契约。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from application.dto import UsageGroup, UsageCall, UsageSeries, UsageSummary
from application.errors import NotFoundError
from application.usage import cache_hit_rate
from runtime.usage_store import window_start

if TYPE_CHECKING:
    from application.ports import ThreadMetadataReader, UsageLedger
    from config import AppConfig

logger = logging.getLogger(__name__)

_VALID_GROUP_BY = ("model", "thread", "day")
"""允许的聚合维度；与 ``runtime.usage_store`` 的白名单保持一致。"""

_DEFAULT_SERIES_ITEMS = 50
"""「按次」视角默认返回的条数。"""

_MAX_SERIES_ITEMS = 200
"""「按次」视角允许请求的最大条数。

WHY 与存储层上限同值却仍在这里拦：这一层拦能给调用方一个明确的 400（告诉他
参数该改多少），存储层那道是数据边界、防的是绕过服务层的直接调用。两处的职责
不同，因此都保留——但取值必须一致，否则会出现「服务层放行、存储层报错」的错位。
"""


class UsageService:
    """按模型 / 会话 / 时间窗聚合 token 用量。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        usage_store: UsageLedger,
        thread_store: ThreadMetadataReader,
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
        *,
        thread_id: str | None = None,
        days: int | None = None,
        group_by: str = "model",
    ) -> UsageSummary:
        """汇总指定范围内的用量。

        Args:
            thread_id: 只统计该会话；``None`` 表示不限会话。
            days: 统计窗口天数；``None`` 表示取配置默认值。
            group_by: 聚合维度，``model`` / ``thread`` / ``day``。

        Returns:
            窗口内的用量汇总与分组明细。

        Raises:
            ValueError: ``days`` 或 ``group_by`` 非法（调用方应映射为 400）。
            NotFoundError: ``thread_id`` 指向的会话不存在。
            RuntimeError: 查询失败（调用方应映射为 500）。
        """
        window_days = self._resolve_days(days)
        if group_by not in _VALID_GROUP_BY:
            raise ValueError(f"group_by 必须是 {list(_VALID_GROUP_BY)} 之一，实际：{group_by}")

        normalized_thread: str | None = None
        if thread_id is not None:
            normalized_thread = await self._normalize_thread(thread_id)

        since = window_start(window_days)

        try:
            result = await self._usage_store.summarize(
                owner_id=None,
                thread_id=normalized_thread,
                since=since,
                group_by=group_by,
            )
        except (ValueError, NotFoundError):
            # WHY 让参数错误与会话不存在原样透出：路由层靠类型把它们分别映射为
            # 400 / 404。若在这里包成 RuntimeError，前端就会把「会话不存在」
            # 当成一次服务故障。
            raise
        except Exception as exc:
            logger.exception(
                "用量聚合失败：thread=%s days=%s group_by=%s",
                normalized_thread,
                window_days,
                group_by,
            )
            raise RuntimeError("用量聚合失败") from exc

        groups = [
            UsageGroup(
                **{
                    **item,
                    # WHY 命中率在这里补而不由存储层返回：``runtime`` 层不得反向
                    # 依赖 ``application``（分层方向是 application → runtime），
                    # 而命中率的定义只该有一处——聚合出的原始计数在应用层换算。
                    "cache_hit_rate": cache_hit_rate(
                        prompt_tokens=int(item.get("prompt_tokens") or 0),
                        cache_hit_tokens=int(item.get("cache_hit_tokens") or 0),
                    ),
                }
            )
            for item in result.get("groups") or []
        ]
        prompt_total = int(result.get("prompt_tokens") or 0)
        cache_hit_total = int(result.get("cache_hit_tokens") or 0)
        summary = UsageSummary(
            window_days=window_days,
            since=since,
            group_by=group_by,
            thread_id=normalized_thread,
            prompt_tokens=prompt_total,
            completion_tokens=int(result.get("completion_tokens") or 0),
            total_tokens=int(result.get("total_tokens") or 0),
            cache_hit_tokens=cache_hit_total,
            cache_miss_tokens=int(result.get("cache_miss_tokens") or 0),
            cache_hit_rate=cache_hit_rate(
                prompt_tokens=prompt_total, cache_hit_tokens=cache_hit_total
            ),
            call_count=int(result.get("call_count") or 0),
            groups=groups,
        )
        logger.debug(
            "用量汇总完成：window=%d 天 total=%d runs=%d cache_hit_rate=%s",
            window_days,
            summary.total_tokens,
            summary.call_count,
            f"{summary.cache_hit_rate:.1%}" if summary.cache_hit_rate is not None else "未知",
        )
        return summary

    async def series(
        self,
        *,
        thread_id: str | None = None,
        days: int | None = None,
        limit: int | None = None,
    ) -> UsageSeries:
        """按时间正序返回最近的逐次调用用量（「按次」视角）。

        WHY 与 ``summarize`` 并列而不是合并：两者回答的问题不同——聚合回答
        「一共花了多少」，序列回答「它是怎么变成这个数的」。合并后返回类型只能
        写成联合体，调用方每次都要先判断拿到的是哪一种。

        Args:
            thread_id: 只统计该会话；``None`` 表示不限会话。
            days: 统计窗口天数；``None`` 表示取配置默认值。
            limit: 返回条数上限；``None`` 表示取默认值。

        Returns:
            窗口内按时间正序排列的逐次调用记录，并标出是否被条数上限截断。

        Raises:
            ValueError: ``days`` / ``limit`` 非法（调用方应映射为 400）。
            NotFoundError: ``thread_id`` 指向的会话不存在（调用方应映射为 404）。
            RuntimeError: 查询失败（调用方应映射为 500）。
        """
        window_days = self._resolve_days(days)
        items_limit = self._resolve_limit(limit)

        normalized_thread: str | None = None
        if thread_id is not None:
            normalized_thread = await self._normalize_thread(thread_id)

        since = window_start(window_days)

        try:
            rows, truncated = await self._usage_store.list_recent(
                owner_id=None,
                thread_id=normalized_thread,
                since=since,
                limit=items_limit,
            )
        except (ValueError, NotFoundError):
            # 与 summarize 同一理由：参数错误与会话不存在要原样透出，路由层靠类型
            # 把它们分别映射为 400 / 404；包成 RuntimeError 会把「会话不存在」
            # 变成一次服务故障。
            raise
        except Exception as exc:
            logger.exception(
                "用量序列查询失败：thread=%s days=%s limit=%s",
                normalized_thread,
                window_days,
                items_limit,
            )
            raise RuntimeError("用量序列查询失败") from exc

        calls = [self._to_call(row) for row in rows]
        logger.debug(
            "用量序列完成：thread=%s 返回 %d 条（截断=%s）",
            normalized_thread,
            len(calls),
            truncated,
        )
        return UsageSeries(
            window_days=window_days,
            since=since,
            thread_id=normalized_thread,
            limit=items_limit,
            count=len(calls),
            truncated=truncated,
            items=calls,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _to_call(row: dict[str, Any]) -> UsageCall:
        """把存储层的一行原始记录转成对外契约。

        WHY 命中率在这里重算而不由存储层给出：``runtime`` 不得反向依赖
        ``application``，而命中率的定义只该有一处——它与聚合视角共用
        ``application.usage.cache_hit_rate``，保证两个视角下同一个数是同一个值。
        """
        prompt_tokens = int(row.get("prompt_tokens") or 0)
        cache_hit_tokens = int(row.get("cache_hit_tokens") or 0)
        completion_tokens = int(row.get("completion_tokens") or 0)
        return UsageCall(
            created_at=str(row.get("created_at") or ""),
            thread_id=str(row.get("thread_id") or ""),
            model=str(row.get("model") or ""),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=int(row.get("cache_miss_tokens") or 0),
            cache_hit_rate=cache_hit_rate(
                prompt_tokens=prompt_tokens, cache_hit_tokens=cache_hit_tokens
            ),
        )

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

    def _resolve_limit(self, limit: int | None) -> int:
        """校验并解析「按次」视角的条数上限。

        WHY 在这里而不是路由层校验：路由层的 Query 约束只拦得住 HTTP 形态的调用，
        而 CLI 与测试也会直接调服务；把规则放在服务层，两种入口得到同一份口径。
        """
        resolved = _DEFAULT_SERIES_ITEMS if limit is None else limit
        if not isinstance(resolved, int) or isinstance(resolved, bool):
            raise ValueError(f"limit 必须是整数，实际：{type(resolved).__name__}")
        if resolved < 1 or resolved > _MAX_SERIES_ITEMS:
            raise ValueError(
                f"limit 必须在 1..{_MAX_SERIES_ITEMS} 之间，实际：{resolved}"
            )
        return resolved

    async def _normalize_thread(self, thread_id: str) -> str:
        """校验会话 ID 并确认该会话存在，返回规范化后的取值。

        WHY 必须回读会话元数据：用量表里既有 ``owner_id`` 也有 ``thread_id``，
        只按会话 ID 过滤会接受一个不存在的 ID 并返回空汇总——那与「这条会话确实
        没有用量」在响应上完全一样，而前者其实是调用方写错了 ID。
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

        if record is None:
            raise NotFoundError("会话", normalized)
        return normalized

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志排错
        return f"UsageService(default_days={self._config.usage_default_window_days})"


__all__ = ["UsageService"]
