"""统一事件模型。

CLI 与 Web 消费同一套事件定义，避免同一件事实在两处各写一遍渲染逻辑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SSEEventType(StrEnum):
    """SSE 事件类型。

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
class SSEEvent:
    """一条 SSE 帧。

    WHY 冻结数据类：事件一旦生成就不应被下游修改，可避免共享状态导致的
    难以复现的渲染错乱。
    """

    event: SSEEventType
    payload: dict[str, Any]

    def encode(self) -> str:
        """序列化为 SSE 文本帧。

        WHY 手动拼帧而非依赖框架：原生 ``EventSource`` 只支持 GET，
        这里必须走 POST + ReadableStream，因此由服务端保证帧格式正确。

        WHY ``ensure_ascii=False``：中文内容若被转义成 \\uXXXX，虽然可解析，
        但会让 SSE 帧体积翻倍，也妨碍调试时肉眼阅读。
        """
        body = json.dumps(self.payload, ensure_ascii=False, default=str)
        # WHY 每行都要独立换行：SSE 规范用空行分隔帧，多行数据会产生多帧
        return f"event: {self.event.value}\ndata: {body}\n\n"
