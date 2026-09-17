"""正在执行的命令登记处：让「停止运行」能顺着会话找到那棵进程树。

WHY 需要这一层：``stop`` 目前只是**接口层确认**——用户按下停止、UI 复位后，
真正跑着的 shell 命令仍在后台活到自己的超时（最长一个 ``SANDBOX_TIMEOUT``）。
实测（见开发计划 T2 风险验证）：取消只能撤掉「等待线程结果」的 future，
无法波及已经在跑的同步 ``execute``。

登记处的形状因此被两个约束决定：

1. **作用域来自 contextvar**，而不是把 ``thread_id`` 一路透传到 backend——
   ``execute`` 是同步工具函数，它的调用链（LangGraph 节点 → backend →
   本模块）上没有会话的概念，硬加参数要改上游协议；而 LangGraph 的同步节点
   由 ``run_in_executor(copy_context)`` 调度，contextvar 天然可见。
2. **登记处是全局的**：终止请求来自事件循环线程，终止动作落在工作线程里，
   二者只能通过进程级共享状态相遇，故用一把锁保护的字典。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol

logger = logging.getLogger(__name__)

_scope: ContextVar[str | None] = ContextVar("execution_scope", default=None)
"""当前执行所属的会话 ID；未绑定时为 ``None``。"""

_guard = threading.Lock()
_registry: dict[str, set[AbortHandle]] = {}
"""会话 ID → 该会话下正在执行的命令句柄集合。

WHY 用集合而不是单个句柄：同一会话在某些工具（并行 subagent）下可能同时
跑着多条命令，只留一个会让「停止」漏掉其中一部分。
"""


class AbortHandle(Protocol):
    """可被中止的一次命令执行。"""

    def abort(self) -> None:
        """立即终止本次执行对应的进程树；已结束时是 no-op。"""


def current_scope() -> str | None:
    """当前执行所属的作用域（会话 ID）；未绑定时为 ``None``。"""
    return _scope.get()


@contextmanager
def bound_scope(scope: str | None) -> Iterator[None]:
    """把后续执行绑定到某个作用域。

    Args:
        scope: 会话 ID；``None`` 表示不绑定（此期间登记会被忽略）。

    WHY 用上下文管理器而不是裸 ``set``：作用域必须在异常路径上也复位。
    漏掉复位会让下一次运行「继承」上一次的会话 ID，从而把 A 会话的进程树
    算到 B 会话头上。
    """
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def register(handle: AbortHandle) -> bool:
    """把一次执行登记到当前作用域。

    Args:
        handle: 可被中止的执行句柄。

    Returns:
        是否真的登记；未绑定作用域时返回 ``False``（例如 CLI 单次调用），
        此时调用方无需反登记。
    """
    scope = _scope.get()
    if scope is None:
        return False
    with _guard:
        _registry.setdefault(scope, set()).add(handle)
    return True


def unregister(handle: AbortHandle) -> None:
    """从当前作用域移除一次执行；未登记时是 no-op。"""
    scope = _scope.get()
    if scope is None:
        return
    with _guard:
        handles = _registry.get(scope)
        if handles is None:
            return
        handles.discard(handle)
        if not handles:
            del _registry[scope]


def abort_scope(scope: str) -> int:
    """中止某个作用域下所有正在执行的命令。

    Args:
        scope: 会话 ID。

    Returns:
        收到终止通知的句柄数；``0`` 表示该会话此刻没有在跑的命令
        （正常现象：停止时图可能正在等模型响应而非执行命令）。

    WHY 单个句柄失败不影响其他句柄：终止是一次性动作，一个句柄抛错
    （进程已退出、句柄已关闭）不应让同会话的其他进程树逃过终止。
    """
    with _guard:
        handles = list(_registry.get(scope) or ())
    if not handles:
        return 0

    logger.info("中止会话 %s 的 %d 个在跑命令", scope, len(handles))
    aborted = 0
    for handle in handles:
        try:
            handle.abort()
        except Exception:
            logger.warning("中止执行句柄失败：scope=%s handle=%r", scope, handle, exc_info=True)
            continue
        aborted += 1
    return aborted
