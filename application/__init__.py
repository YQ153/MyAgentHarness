"""应用层：把 LangGraph 的内部事件翻译成稳定的对外协议。

对外提供三件事，彼此职责互不重叠：

- :class:`ThreadService` 会话的元数据生命周期（清单、历史、删除）
- :class:`RunService`   一次运行的推进与事件产出
- :class:`ModelCatalog` 可切换模型的只读目录
"""

from application.dto import (
    DeleteOutcome,
    DeleteResult,
    HistoryMessage,
    ModelInfo,
    ThreadListResult,
    ThreadSummary,
)
from application.errors import ThreadBusyError
from application.events import AgentEvent, AgentEventType
from application.model_catalog import ModelCatalog
from application.run_service import RunService
from application.thread_service import ThreadService

__all__ = [
    "AgentEvent",
    "AgentEventType",
    "DeleteOutcome",
    "DeleteResult",
    "HistoryMessage",
    "ModelCatalog",
    "ModelInfo",
    "RunService",
    "ThreadBusyError",
    "ThreadListResult",
    "ThreadService",
    "ThreadSummary",
]
