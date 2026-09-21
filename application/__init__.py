"""应用层：把内核与基础设施的能力编排成稳定的对外业务契约。

WHY 分成「职责域」与「对外契约」两段写：本层有 30 个模块，而 ``__all__``
只导出 12 个符号。旧写法（「对外提供三件事」）会让新读者以为这一层只有三个
东西，从而漏掉二十余个模块——按它理解代码，会找不到附件、知识库、记忆、
技能、用量、工作区、健康、分叉、治理与运行登记。

职责域（层内模块，不被 ``__all__`` 导出）：

- 会话与运行编排：``thread_service`` / ``run_service`` / ``run_branch`` /
  ``run_governance`` / ``run_registry``
- 领域能力服务：``attachment_service`` / ``knowledge_service`` /
  ``memory_service`` / ``skill_service`` / ``usage_service`` /
  ``workspace_service`` / ``session_registry`` / ``model_catalog`` /
  ``tool_catalog`` / ``health``
- 对外契约：``dto`` / ``errors`` / ``events`` / ``ports`` /
  ``audit_recorder``
- 协议翻译与支撑：``event_translator`` / ``interrupt_codec`` / ``usage`` /
  ``runnable`` / ``thread_export`` / ``message_utils`` / ``audit_context``

对外导出的稳定契约只有 ``__all__`` 中的 12 个符号，其余请按具体模块路径导入。
``__init__`` 保持最小导出面是有意的：扩大它会把这二十余个模块一并变成
「公共 API 面」，此后任何调整都成了对接口层的破坏性变更。

分组口径与 ``tests/application/test_layer_purity_contract.py`` 的层内纯度契约
一致——两处必须同时改，否则契约会先失败（这是有意的）。
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
