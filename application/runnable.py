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


def build_runnable_config(
    config: AppConfig,
    thread_id: str,
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    """构造 LangGraph 运行配置。

    WHY 必须设 recursion_limit：通用 Agent 的长任务可能触发深层循环，
    默认值过小会导致任务在中途被硬性终止。

    WHY 支持指定 ``checkpoint_id``：这是分叉的唯一入口。带上它去运行，LangGraph 会
    以该检查点为起点写出**新**检查点，旧路径原样保留（已由
    ``scripts/probe_checkpoint_fork.py`` 真机验证）。不带它则是「接着当前分支的头跑」。

    WHY 不在这里补 ``checkpoint_ns``：只有 ``update_state`` 那条写接口需要它，而分叉
    走的是「以历史检查点为起点重跑」，不需要写状态——少一个字段就少一处口径分叉。

    Args:
        config: 应用配置，提供 ``recursion_limit``。
        thread_id: 会话 ID，作为检查点的键。
        checkpoint_id: 分叉起点；``None`` 表示沿用该会话当前的头部。

    Returns:
        可直接传给 ``graph.astream`` / ``graph.aget_state`` 的配置字典。

    Raises:
        ValueError: ``config`` 为 ``None`` 或 ``thread_id`` 为空。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    if not thread_id:
        raise ValueError("thread_id 不能为空")

    configurable: dict[str, Any] = {"thread_id": thread_id}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id

    return {
        "configurable": configurable,
        "recursion_limit": config.recursion_limit,
    }
