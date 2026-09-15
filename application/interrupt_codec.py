"""HITL 中断载荷的编解码。

把中断/恢复的复杂结构收敛到这一处的原因：载荷格式由 langchain 的
``HumanInTheLoopMiddleware`` 严格定义（``HITLRequest`` / ``HITLResponse``），
一旦前后端各自拼装，极易出现「前端发了 approve，后端期望 decisions 列表」
这类只在运行期暴露的错误。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from langgraph.types import Command

logger = logging.getLogger(__name__)

DecisionType = Literal["approve", "edit", "reject", "respond"]

_VALID_DECISIONS: frozenset[str] = frozenset({"approve", "edit", "reject", "respond"})


@dataclass(frozen=True)
class InterruptRequest:
    """一次待人工处理的中断。"""

    interrupt_id: str
    action_requests: list[dict[str, Any]] = field(default_factory=list)
    review_configs: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """转成前端可直接渲染的结构。"""
        return {
            "interrupt_id": self.interrupt_id,
            "action_requests": self.action_requests,
            "review_configs": self.review_configs,
        }


def decode_interrupt(update: Any) -> InterruptRequest | None:
    """从 LangGraph 的 updates 载荷中提取中断信息。

    LangGraph 在中断时产出 ``{"__interrupt__": (Interrupt(value=..., id=...),)}``，
    其中 HITL 场景下 ``value`` 就是 ``HITLRequest`` 字典。

    Args:
        update: ``stream_mode="updates"`` 产出的单次载荷。

    Returns:
        解析成功返回中断请求；非中断载荷返回 ``None``。
    """
    if not isinstance(update, dict):
        return None

    raw = update.get("__interrupt__")
    if raw is None:
        return None

    # 元组或列表取首个元素，兼容不同版本的产出形态
    first = raw[0] if isinstance(raw, (tuple, list)) and raw else raw
    if first is None:
        return None

    value = getattr(first, "value", None)
    interrupt_id = str(getattr(first, "id", "") or "")

    if isinstance(value, dict):
        return InterruptRequest(
            interrupt_id=interrupt_id,
            action_requests=list(value.get("action_requests") or []),
            review_configs=list(value.get("review_configs") or []),
        )

    # 非 HITL 中断（业务代码里的裸 interrupt()）：包成统一形状，
    # 让前端只处理一种结构，不必区分来源。
    logger.warning("收到非 HITL 形态的中断，已做适配：%r", type(value).__name__)
    return InterruptRequest(
        interrupt_id=interrupt_id,
        action_requests=[
            {
                "name": "interrupt",
                "args": {},
                "description": str(value),
            }
        ],
        review_configs=[
            {
                "action_name": "interrupt",
                "allowed_decisions": ["approve", "reject"],
            }
        ],
    )


def normalize_decisions(raw: Any) -> list[dict[str, Any]]:
    """把外部提交的审批结果规整为中间件期望的 decisions 列表。

    Args:
        raw: 形如 ``{"decisions": [{"type": "approve"}]}`` 的载荷，
            也允许直接传单个 decision 字典或 decisions 列表。

    Returns:
        标准化的 decisions 列表。

    Raises:
        ValueError: 载荷不是对象、decision 类型非法或缺少必需字段。
    """
    if isinstance(raw, dict):
        items = raw.get("decisions", raw)
    else:
        items = raw

    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        raise ValueError("审批载荷格式非法：需要非空的对象或数组")

    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"每条审批结果必须是对象，实际：{type(item).__name__}")

        decision_type = item.get("type")
        if decision_type not in _VALID_DECISIONS:
            raise ValueError(
                f"未知审批类型 {decision_type!r}，可选：{sorted(_VALID_DECISIONS)}"
            )

        decision: dict[str, Any] = {"type": decision_type}

        if decision_type == "edit":
            edited = item.get("edited_action")
            if not isinstance(edited, dict) or not edited.get("name"):
                raise ValueError("edit 类型必须提供 edited_action.name")
            decision["edited_action"] = {
                "name": edited["name"],
                "args": edited.get("args", {}),
            }

        if decision_type in ("reject", "respond"):
            # 拒绝与代答都依赖 message：中间件会把它回灌给模型作为反馈
            message = item.get("message")
            decision["message"] = message if isinstance(message, str) else ""

        normalized.append(decision)

    return normalized


def build_resume_command(raw: Any) -> Command:
    """构造恢复执行的 ``Command``。

    WHY 包一层 ``{"decisions": [...]}``：这是 ``HITLResponse`` 的唯一合法形状，
    直接传列表或裸字典会被引擎拒绝。
    """
    decisions = normalize_decisions(raw)
    logger.info("构造恢复指令：decisions=%d", len(decisions))
    return Command(resume={"decisions": decisions})
