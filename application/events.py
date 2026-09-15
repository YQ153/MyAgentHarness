"""统一事件模型。

CLI 与 Web 消费同一套事件定义，避免同一件事实在两处各写一遍渲染逻辑。

WHY 命名为 ``AgentEvent`` 而非 ``SSEEvent``：本模块位于应用层，描述的是
「运行过程中发生了什么」，与传输方式无关——CLI 直接渲染到终端，Web 走 SSE。
以 SSE 命名会把应用层绑死在 HTTP 传输上；序列化成 SSE 文本帧的职责已下沉到
``interfaces.web.sse``。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AgentEventType(StrEnum):
    """运行事件类型。

    命名与前端约定一一对应，前端据此分派渲染。
    """

    TOKEN = "token"
    """增量文本片段。"""

    TOOL_CALL = "tool_call"
    """一次完整的工具调用（含拼接完成的参数）。"""

    TOOL_RESULT = "tool_result"
    """工具执行结果。"""

    TODOS = "todos"
    """待办列表快照。"""

    STEP = "step"
    """节点级进度。"""

    INTERRUPT = "interrupt"
    """请求人工审批，携带 action_requests 与 review_configs。"""

    ERROR = "error"
    """运行期错误。"""

    DONE = "done"
    """本轮运行结束。"""


@dataclass(frozen=True)
class AgentEvent:
    """一条运行事件。

    WHY 冻结数据类：事件一旦生成就不应被下游修改，可避免共享状态导致的
    难以复现的渲染错乱。
    """

    event: AgentEventType
    payload: dict[str, Any]
