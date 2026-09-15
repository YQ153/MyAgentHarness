"""Web 接口的请求与响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EditedAction(BaseModel):
    """审批时改写后的工具调用。"""

    name: str = Field(description="工具名称")
    args: dict[str, Any] = Field(default_factory=dict, description="改写后的参数")


class DecisionPayload(BaseModel):
    """单条人工审批结果。

    四种类型与 langchain ``HITLResponse`` 的 Decision 定义一一对应：
    approve 按原样执行、edit 改写后执行、reject 拒绝、respond 由人代答。
    """

    type: Literal["approve", "edit", "reject", "respond"]
    message: str | None = Field(default=None, description="拒绝或代答时给模型的反馈")
    edited_action: EditedAction | None = Field(default=None, description="改写后的调用")


class ChatRequest(BaseModel):
    """发起一轮对话。"""

    content: str = Field(min_length=1, description="用户输入")
    model: str | None = Field(default=None, description="模型别名，None 表示默认模型")


class ResumeRequest(BaseModel):
    """人工审批后恢复执行。"""

    decisions: list[DecisionPayload] = Field(min_length=1, description="审批结果列表")
    model: str | None = Field(default=None, description="模型别名，None 表示默认模型")


class ThreadResponse(BaseModel):
    """新会话的标识。"""

    thread_id: str


class ThreadSummary(BaseModel):
    """会话列表中的一条记录。

    WHY 时间字段用字符串而非 ``datetime``：库中存的就是定宽 ISO8601 文本，
    直接透出可以避免一次无意义的正反序列化；同时也让前端无需处理时区转换
    （后端统一写 UTC 并带 ``+00:00`` 偏移）。
    """

    thread_id: str = Field(description="会话标识")
    title: str = Field(description="会话标题，尚未产生首轮对话时为空串")
    created_at: str = Field(description="创建时间（ISO8601 UTC）")
    updated_at: str = Field(description="最近活动时间（ISO8601 UTC）")
    turn_count: int = Field(description="已发生的用户对话轮数")


class ThreadListResponse(BaseModel):
    """会话清单及其总数。"""

    items: list[ThreadSummary] = Field(description="按最近活动时间倒序排列的会话")
    total: int = Field(description="会话总数，用于分页展示")


class DeleteResponse(BaseModel):
    """删除结果。"""

    thread_id: str
    deleted: bool


class ModelInfo(BaseModel):
    """模型展示信息，不含任何密钥。"""

    name: str
    provider: str
    model: str


class HistoryMessage(BaseModel):
    """一条历史消息。"""

    role: str
    content: str
    name: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
