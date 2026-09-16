"""应用层对外的返回对象（DTO）。

WHY 用 pydantic 模型而不是裸 dict：服务层与接口层之间必须有一份编译期可检查
的契约。返回 ``dict[str, Any]`` 时，字段的增删与类型变更只能在运行期暴露
（表现为 KeyError 或前端渲染空白），且 FastAPI 无法据此生成 OpenAPI 文档。

WHY 只放「服务层产出」的模型：请求体（``ChatRequest`` 等）是接口层关心的事，
留在 ``interfaces.web.schemas``，避免应用层被 HTTP 语义污染。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DeleteOutcome(StrEnum):
    """删除会话的结果分类。

    WHY 不用布尔值：``False`` 会同时表示「会话本来就不存在」「元数据删除失败」
    「检查点清理失败」三种含义，调用方无法区分「没删到」与「删失败了」，
    也就无法决定是提示用户还是重试。
    """

    DELETED = "deleted"
    """元数据与检查点均已清理。"""

    NOT_FOUND = "not_found"
    """该会话从未登记过，未发生任何删除。"""

    PARTIAL = "partial"
    """元数据已删除，但检查点清理失败（残留历史需要人工介入）。"""

    FAILED = "failed"
    """元数据删除失败，会话仍然存在。"""


class ModelInfo(BaseModel):
    """模型展示信息，不含任何密钥。"""

    name: str = Field(description="模型别名，切换模型时使用")
    provider: str = Field(description="langchain provider 标识")
    model: str = Field(description="provider 侧的原始模型名")


class ThreadSummary(BaseModel):
    """会话列表中的一条记录。

    WHY 时间字段用字符串而非 ``datetime``：库中存的就是定宽 ISO8601 文本，
    直接透出可以避免一次无意义的正反序列化；同时也让前端无需处理时区转换
    （后端统一写 UTC 并带 ``+00:00`` 偏移）。
    """

    thread_id: str = Field(description="会话标识")
    owner_id: str = Field(default="", description="所有者用户标识，未认证场景为空串")
    title: str = Field(description="会话标题，尚未产生首轮对话时为空串")
    created_at: str = Field(description="创建时间（ISO8601 UTC）")
    updated_at: str = Field(description="最近活动时间（ISO8601 UTC）")
    turn_count: int = Field(description="已发生的用户对话轮数")


class ThreadListResult(BaseModel):
    """会话清单及其总数。"""

    items: list[ThreadSummary] = Field(description="按最近活动时间倒序排列的会话")
    total: int = Field(description="会话总数，用于分页展示")


class HistoryMessage(BaseModel):
    """一条历史消息。"""

    role: str = Field(description="消息角色，如 human / ai / tool")
    content: str = Field(description="消息正文")
    name: str = Field(default="", description="工具名，非工具消息为空串")
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list, description="该消息发起的工具调用"
    )


class DeleteResult(BaseModel):
    """删除会话的结果。"""

    thread_id: str = Field(description="被删除的会话标识")
    outcome: DeleteOutcome = Field(description="删除结果分类")
    checkpoint_removed: bool = Field(description="检查点是否已清理")
    detail: str = Field(default="", description="失败原因摘要，成功时为空串")

    @property
    def deleted(self) -> bool:
        """元数据是否确实被移除（用于接口层的布尔语义兼容）。"""
        return self.outcome is DeleteOutcome.DELETED
