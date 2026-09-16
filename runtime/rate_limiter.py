"""内存级令牌桶限流器。

职责边界：为认证端点提供按 IP 的粗粒度限流，防止暴力破解与重放攻击。
WHY 内存实现：单节点/小集群场景够用；多实例负载均衡下需要在网关或 Redis 层统一限流，
本模块作为「兜底最后一道防线」存在。

实现要点：
- 滑动窗口计数，窗口结束时清理过期记录。
- 使用 ``threading.Lock`` 保护桶表：操作极短，且清理动作需要同步完成。
- 不持久化，进程重启后计数清零。

WHY 位于 runtime 层：本模块不含任何业务语义，只依赖标准库的 ``threading``
与 ``time``，属于基础设施组件。若留在 application 层，接口层为获取它必须
依赖应用层（语义错位）；下沉到 runtime 后，未来 runtime 内部需要限流时也不会
形成 ``runtime → application`` 的反向依赖。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class _Bucket:
    """单个 IP 的计数桶。"""

    window_start: float
    count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class RateLimiter:
    """基于滑动窗口的 IP 限流器。"""

    def __init__(self, *, window_seconds: float, max_attempts: int) -> None:
        """构造限流器。

        Args:
            window_seconds: 窗口长度（秒）。
            max_attempts: 每个窗口内允许的最大请求数。

        Raises:
            ValueError: 参数非法。
        """
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须为正数")
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须为正数")

        self._window_seconds = window_seconds
        self._max_attempts = max_attempts
        self._buckets: dict[str, _Bucket] = defaultdict(
            lambda: _Bucket(window_start=time.monotonic())
        )
        self._global_lock = threading.Lock()

    def _now(self) -> float:
        return time.monotonic()

    def is_allowed(self, key: str) -> bool:
        """判断某 key 是否仍可在当前窗口内发起请求。

        首次调用或窗口过期时自动开新窗口。
        """
        if not isinstance(key, str):
            return False

        now = self._now()
        with self._global_lock:
            bucket = self._buckets[key]

        with bucket.lock:
            if now - bucket.window_start > self._window_seconds:
                bucket.window_start = now
                bucket.count = 0
            if bucket.count >= self._max_attempts:
                logger.warning("触发限流：key=%s count=%s window=%ss", key, bucket.count, self._window_seconds)
                return False
            bucket.count += 1
            return True

    def cleanup(self) -> int:
        """清理已过期窗口，返回清理数量。"""
        now = self._now()
        removed = 0
        with self._global_lock:
            keys = list(self._buckets.keys())
            for key in keys:
                bucket = self._buckets[key]
                with bucket.lock:
                    if now - bucket.window_start > self._window_seconds:
                        del self._buckets[key]
                        removed += 1
        return removed

    def reset(self, key: str) -> None:
        """重置某 key 的计数，用于测试或人工解封。"""
        with self._global_lock:
            self._buckets.pop(key, None)
