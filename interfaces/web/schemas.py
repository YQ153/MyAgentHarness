"""Web 接口的请求与响应模型。

WHY 只在这里定义「请求体」与「HTTP 专属的响应包装」：服务层产出的数据结构
定义在 ``application.dto``，那是本应用对外的稳定契约。响应模型若在这里再抄一份
字段，两处迟早漂移——改了 DTO 忘了改 schema，接口就会静默少字段。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from application.dto import DeleteOutcome, HistoryMessage, ModelInfo, ThreadSummary


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


class ThreadUpdateRequest(BaseModel):
    """会话的局部更新：重命名与归档。

    WHY 用一个 PATCH 承载两件事：两者都是「所有者对清单条目的整理」，且都可能
    在同一处界面动作里发生（改完名顺手归档）；拆成两个端点只会让前端多一次
    往返，也让「至少改一项」这条校验要在两处各写一遍。

    WHY 两个字段都可选但必须至少给一个：只传 None 表示一次什么都没改的请求，
    应当直接拒绝（400），否则会产出「接口返回成功但没有任何变化」这种无法归因
    的结果。
    """

    title: str | None = Field(default=None, description="新标题；None 表示不改标题")
    archived: bool | None = Field(default=None, description="归档状态；None 表示不改归档")
    tags: list[str] | None = Field(
        default=None, description="整体替换标签；None 表示不改标签，空列表表示清空"
    )


class ChatRequest(BaseModel):
    """发起一轮对话。"""

    content: str = Field(min_length=1, description="用户输入")
    model: str | None = Field(default=None, description="模型别名，None 表示默认模型")


class RegenerateRequest(BaseModel):
    """重新生成最后一轮助手回复。"""

    model: str | None = Field(default=None, description="模型别名，None 表示默认模型")


class EditRequest(BaseModel):
    """编辑指定轮次的用户消息并从该点分叉。"""

    message_index: int = Field(
        ge=0, description="目标消息在当前分支消息列表中的下标（0 基），必须指向用户消息"
    )
    content: str = Field(min_length=1, description="改写后的用户消息")
    model: str | None = Field(default=None, description="模型别名，None 表示默认模型")


class ResumeRequest(BaseModel):
    """人工审批后恢复执行。"""

    decisions: list[DecisionPayload] = Field(min_length=1, description="审批结果列表")
    model: str | None = Field(default=None, description="模型别名，None 表示默认模型")


class StopResponse(BaseModel):
    """停止运行请求的结果。

    WHY 幂等语义由 ``reason`` 表达而不是状态码：``not_running`` 与
    ``already_stopping`` 都是「意图已满足」的正常结果，用 200 + 明细
    让前端可以区分提示，而不是把连点停止当成冲突报错。
    """

    thread_id: str
    stopped: bool = Field(description="是否已对运行中的会话触发停止")
    reason: str = Field(
        description="结果分类：requested（本次触发）/ "
        "already_stopping（此前已触发）/ not_running（当前无运行）"
    )


class HealthResponse(BaseModel):
    """存活探测结果。

    WHY 不带任何依赖状态：存活探测的语义是「进程还在不在」，一旦掺入数据库
    之类的依赖判断，一次依赖抖动就会让编排系统重启一个其实健康的进程。
    """

    status: Literal["ok"] = Field(description="进程存活标识，恒为 ok")
    uptime_seconds: float = Field(description="进程已运行秒数")


class ThreadResponse(BaseModel):
    """新会话的标识。"""

    thread_id: str


class ThreadListResponse(BaseModel):
    """会话清单及其总数。"""

    items: list[ThreadSummary] = Field(description="按最近活动时间倒序排列的会话")
    total: int = Field(description="会话总数，用于分页展示")


class DeleteResponse(BaseModel):
    """删除结果。

    WHY 同时给出 ``deleted`` 与 ``outcome``：``deleted`` 保持既有前端可读的布尔
    语义（会话是否已从清单移除），``outcome`` 则暴露「不存在 / 部分成功 / 失败」
    的区分，让调用方能判断是否需要重试或提示人工介入。
    """

    thread_id: str
    deleted: bool = Field(description="会话是否已从清单中移除")
    outcome: DeleteOutcome = Field(description="删除结果分类")
    detail: str = Field(default="", description="失败原因摘要，成功时为空串")


__all__ = [
    "ChatRequest",
    "DecisionPayload",
    "DeleteResponse",
    "EditedAction",
    "HealthResponse",
    "HistoryMessage",
    "ModelInfo",
    "ResumeRequest",
    "StopResponse",
    "ThreadListResponse",
    "ThreadResponse",
    "ThreadUpdateRequest",
]
