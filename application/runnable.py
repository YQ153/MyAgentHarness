"""LangGraph 运行配置的构造。

WHY 单独成模块：``ThreadService``（读历史）与 ``RunService``（发起运行）都需要
完全相同的运行配置，其中 ``recursion_limit`` 是必须显式设置的关键项。分散到两处
后，一处调整另一处漏改会造成「读历史正常但运行中途被硬性终止」这类难以复现的
差异。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import AppConfig


def build_runnable_config(config: AppConfig, thread_id: str) -> dict[str, Any]:
    """构造 LangGraph 运行配置。

    WHY 必须设 recursion_limit：通用 Agent 的长任务可能触发深层循环，
    默认值过小会导致任务在中途被硬性终止。

    Args:
        config: 应用配置，提供 ``recursion_limit``。
        thread_id: 会话 ID，作为检查点的键。

    Returns:
        可直接传给 ``graph.astream`` / ``graph.aget_state`` 的配置字典。

    Raises:
        ValueError: ``config`` 为 ``None`` 或 ``thread_id`` 为空。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    if not thread_id:
        raise ValueError("thread_id 不能为空")

    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": config.recursion_limit,
    }
