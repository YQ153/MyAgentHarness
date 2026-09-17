"""后台周期任务的通用骨架。

职责边界：只负责「启动、按间隔重复调用一次回调、被取消时干净退出」，
不关心回调做什么（清理审计、巡检运行……），也不决定间隔取值（由配置给出）。

WHY 放在 ``runtime`` 而不是 ``bootstrap``：它与 ``AuditRetentionWorker``
一样是可独立启停、可单独测试的协程；``bootstrap`` 只负责按配置把它装配到
宿主（当前是 Web 形态的 lifespan）上。

WHY 收敛成一个骨架：定时任务的三件事容易各写各的——「启动时先跑一次」
「单次失败不能退出循环」「停止时必须 cancel 后 await」。任一条漏掉的表现
分别是「重启后要等一个间隔才干活」「一次数据库抖动让任务永久停摆」「进程
退出时报 Task was destroyed」。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class IntervalWorker:
    """按固定间隔重复调用一次异步回调的后台任务。

    幂等与可重入：``start`` 重复调用不会起第二个协程；``stop`` 在未启动或
    已停止时是 no-op，因此在 lifespan 的 ``finally`` 中可以无条件调用。

    Args:
        callback: 每轮执行的无参协程函数；其返回值由 ``run_once`` 原样透出。
        interval_seconds: 间隔秒数，必须 >= 1。
        name: 任务名，用于 ``asyncio.Task`` 命名与日志定位。
        detail: 追加在启动日志里的补充信息（如阈值），可为空。

    Raises:
        ValueError: ``callback`` 不可调用、``interval_seconds`` 越界或非整数、
            ``name`` 为空。
    """

    def __init__(
        self,
        callback: Callable[[], Awaitable[Any]],
        *,
        interval_seconds: int,
        name: str = "interval-worker",
        detail: str = "",
    ) -> None:
        if not callable(callback):
            raise ValueError("callback 必须是可调用对象")
        if not isinstance(interval_seconds, int) or isinstance(interval_seconds, bool):
            raise ValueError("interval_seconds 必须是整数")
        if interval_seconds < 1:
            raise ValueError("interval_seconds 不能小于 1")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name 必须是非空字符串")

        self._callback = callback
        self._interval_seconds = interval_seconds
        self._name = name.strip()
        self._detail = detail
        self._task: asyncio.Task[None] | None = None
        self._iterations = 0
        self._failures = 0

    @property
    def name(self) -> str:
        """任务名。"""
        return self._name

    @property
    def interval_seconds(self) -> int:
        """巡检间隔秒数。"""
        return self._interval_seconds

    @property
    def is_running(self) -> bool:
        """协程是否在运行。"""
        return self._task is not None and not self._task.done()

    @property
    def iterations(self) -> int:
        """已执行的轮次（含失败的轮次）。"""
        return self._iterations

    @property
    def failures(self) -> int:
        """执行失败的轮次数。

        WHY 单独计数：回调抛异常不会让循环停下，若不看这个计数，
        「一直在跑但一直失败」与「正常运行」在日志之外无从区分。
        """
        return self._failures

    def start(self) -> None:
        """启动协程并立即执行第一轮；已运行时不做任何事。

        WHY 先立即执行一次：服务重启往往发生在长时间停机之后，堆着待处理的
        状态等到下一个间隔才动手，会让首个间隔内的系统仍处在陈旧状态里。

        Raises:
            RuntimeError: 当前没有运行中的事件循环（在同步上下文里调用）。
        """
        if self.is_running:
            logger.debug("后台任务 %s 已在运行，忽略重复启动", self._name)
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(f"{self._name}.start() 必须在运行中的事件循环内调用") from exc

        self._task = loop.create_task(self._run(), name=self._name)
        logger.info("后台任务 %s 已启动：间隔 %d 秒%s", self._name, self._interval_seconds, self._detail)

    async def stop(self) -> None:
        """停止协程并等待其退出；未启动时直接返回。

        WHY 取消后必须 await：只在 ``finally`` 里 ``cancel()`` 而不等待，
        协程可能仍在执行回调（例如正在 DELETE），进程关闭阶段会撞上已关闭的
        连接，并留下 "Task was destroyed but it is pending" 告警。
        """
        task = self._task
        self._task = None
        if task is None:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.info("后台任务 %s 已停止", self._name)
        except Exception:
            # WHY 记录后吞掉：停止阶段的失败无处可上报，若让它冒泡会掩盖
            # lifespan 中更关键的关闭异常。
            logger.exception("后台任务 %s 异常退出", self._name)
        finally:
            self._task = None

    async def run_once(self) -> Any:
        """执行一轮回调，返回其结果；供测试与外部手动触发使用。"""
        return await self._callback()

    async def _run(self) -> None:
        """主循环：执行 → 等待 → 再执行，直到被取消。

        WHY 单次失败不退出循环：这类任务都是旁路运维能力，一次数据库抖动就
        永久停掉它，会让「以为在自动治理」变成静默的状态堆积；失败后按原
        间隔重试，并把异常留在日志里供告警。
        """
        try:
            while True:
                try:
                    self._iterations += 1
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._failures += 1
                    logger.exception(
                        "后台任务 %s 执行失败，将在 %d 秒后重试",
                        self._name,
                        self._interval_seconds,
                    )
                await asyncio.sleep(self._interval_seconds)
        except asyncio.CancelledError:
            logger.info("后台任务 %s 收到取消信号，正在退出", self._name)
            raise


__all__ = ["IntervalWorker"]
