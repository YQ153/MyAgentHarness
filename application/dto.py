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


class CheckResult(BaseModel):
    """单项依赖探测的结果。

    WHY 不只用布尔值：探测失败时运维需要知道「是数据库连不上，还是默认模型
    没配密钥」，只有布尔值会让排查回到翻日志的老路。
    """

    name: str = Field(description="检查项标识，如 database / model")
    ok: bool = Field(description="该项是否通过")
    detail: str = Field(default="", description="失败原因；通过时为空串")


class ReadinessReport(BaseModel):
    """就绪探测汇总。

    WHY 所有检查项都跑完再汇总而不是首个失败即返回：运维看到的是「哪几项
    不健康」，一次请求拿到全貌远快于逐个试错。
    """

    ready: bool = Field(description="是否可对外提供服务")
    checks: list[CheckResult] = Field(description="各项检查结果")


class MetricsSnapshot(BaseModel):
    """运行指标的瞬时快照。

    WHY 只暴露计数而不暴露会话 ID：指标端点通常不设鉴权（探活与采集系统在
    调用），计数足以支撑容量观察，ID 清单则会把用户的会话活动范围泄漏出去。
    """

    running_threads: int = Field(description="当前运行中的会话数")
    started_runs: int = Field(description="进程启动以来累计发起的运行次数")
    pending_hitl: int = Field(description="等待人工审批的会话数")
    audit_events: int | None = Field(
        default=None, description="审计事件总数；``None`` 表示本次采集失败"
    )
    uptime_seconds: float = Field(description="进程已运行秒数")
