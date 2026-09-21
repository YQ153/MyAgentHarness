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
    attachment_ids: list[str] = Field(
        default_factory=list,
        description="随本轮发送的附件 ID（来自上传接口）；空列表表示纯文本",
    )
    workspace: str | None = Field(
        default=None,
        description=(
            "本条会话要使用的工作区绝对路径；None 表示用启动默认值。"
            "只在会话首条消息上生效——已绑定的会话给出不同取值会被拒绝（409）"
        ),
    )


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


class AttachmentDeleteResponse(BaseModel):
    """删除附件的结果。

    WHY 带 ``deleted`` 而不是直接 204：``deleted=false`` 表示「该附件本就不存在」，
    与「删掉了」是两种不同的既成事实。用 204 抹平之后，前端无法区分「清理成功」
    与「本来就没有」，而这恰恰是并发重试时最需要判断的一件事。
    """

    thread_id: str = Field(description="会话标识")
    attachment_id: str = Field(description="被请求删除的附件标识")
    deleted: bool = Field(description="是否确实删除了；false 表示该附件不存在")


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


class KnowledgeCapabilities(BaseModel):
    """知识库当前生效的能力与参数。

    WHY 与清单一起下发：前端要据此决定「语义检索这一栏显不显示」以及「提示什么」。
    让前端自己猜（或写死）会在换配置后给出与实际不符的说明。
    """

    vector_enabled: bool = Field(description="是否启用向量检索（取决于嵌入后端）")
    embedding_backend: str | None = Field(default=None, description="嵌入后端标识；未启用为 null")
    dims: int = Field(description="向量维度")
    model: str = Field(default="", description="嵌入模型标识")
    chunk_chars: int = Field(description="单个分块的目标字符数")
    chunk_overlap_chars: int = Field(description="相邻分块的重叠字符数")
    top_k: int = Field(description="检索返回的块数上限")


class KnowledgeDocumentInfo(BaseModel):
    """一份已索引的文档。"""

    source_path: str = Field(description="源文件的工作区虚拟路径")
    chunk_count: int = Field(description="该文档的分块数")
    indexed_at: str = Field(description="最近一次索引时刻（ISO8601 UTC）")


class KnowledgeStats(BaseModel):
    """索引规模。"""

    owner_id: str = Field(description="索引归属；认证关闭时为空串")
    document_count: int
    chunk_count: int
    vector_count: int = Field(description="已写入的向量条数；未启用向量检索时为 0")
    vector_enabled: bool
    dims: int
    model: str


class KnowledgeListResponse(BaseModel):
    """``GET /api/knowledge`` 的响应。"""

    items: list[KnowledgeDocumentInfo]
    stats: KnowledgeStats
    capabilities: KnowledgeCapabilities


class KnowledgeIndexRequest(BaseModel):
    """``POST /api/knowledge`` 的请求体。"""

    path: str | None = Field(
        default=None, description="只索引这一份文档（工作区虚拟路径）；留空表示索引整个工作区"
    )
    force: bool = Field(default=False, description="为 true 时忽略内容指纹，强制重建")


class KnowledgeIndexItem(BaseModel):
    """逐份文档的索引结果。"""

    source_path: str
    status: str = Field(description="indexed / unchanged / empty / skipped")
    chunk_count: int = Field(default=0)
    vector_status: str = Field(default="", description="ok / disabled / failed / none / unchanged")
    detail: str = Field(default="", description="跳过原因；非跳过时为空串")


class KnowledgeIndexResponse(BaseModel):
    """``POST /api/knowledge`` 的响应。"""

    scanned: int = Field(description="扫描到的候选文件数")
    indexed: int = Field(description="本次新索引（或强制重建）的文档数")
    unchanged: int = Field(description="内容未变而跳过的文档数")
    empty: int = Field(description="无可索引内容而跳过的文档数")
    skipped: int = Field(description="因二进制 / 编码 / 超限而跳过的文档数")
    items: list[KnowledgeIndexItem]


class KnowledgeDeleteResponse(BaseModel):
    """``DELETE /api/knowledge`` 的响应。"""

    source_path: str
    deleted: bool = Field(description="此前是否已索引（false 表示本来就没索引过）")


class SkillInfo(BaseModel):
    """一个已加载的技能。"""

    name: str
    description: str = ""
    directory: str = Field(description="技能包所在目录的虚拟路径")
    skill_md_path: str = Field(default="", description="``SKILL.md`` 的虚拟路径")
    source: str = Field(default="", description="来自哪个技能来源目录")
    enabled: bool = Field(description="是否参与加载；停用后不再注入模型上下文")
    problems: list[str] = Field(
        default_factory=list,
        description="上游只告警不报错的问题（如 name 与目录名不符）；非空表示该技能形态可疑",
    )


class SkillUnloadable(BaseModel):
    """候选目录里没能加载成技能的那一项。"""

    directory: str = Field(description="目录的虚拟路径")
    reason: str = Field(description="加载失败的原因")


class SkillListResponse(BaseModel):
    """``GET /api/skills`` 的响应。"""

    scope: str = Field(description="状态作用域；当前固定为 global（技能集全应用共享）")
    items: list[SkillInfo]
    unloadable: list[SkillUnloadable] = Field(
        default_factory=list,
        description="被跳过 / 解析失败的候选目录——上游对它们只写日志，不报出来就无从排查",
    )
    load_errors: list[str] = Field(
        default_factory=list, description="来源目录整体读取失败的原因（如目录不可读）"
    )
    view_path: str = Field(default="", description="物化视图目录的绝对路径")
    view_exists: bool = Field(
        default=False, description="视图是否存在；为 false 时建图会退回「全部技能」并告警"
    )
    graph_sources: list[str] = Field(
        default_factory=list, description="下一次装配 Agent 时实际使用的技能来源目录"
    )
    view_warning: str = Field(
        default="", description="视图不可用时的降级说明；非空即表示启停当前不生效"
    )


class SkillToggleRequest(BaseModel):
    """``PATCH /api/skills/{name}`` 的请求体。"""

    enabled: bool = Field(description="true 启用、false 停用")


class SkillToggleResponse(BaseModel):
    """``PATCH /api/skills/{name}`` 的响应。"""

    name: str
    enabled: bool
    scope: str
    updated_at: str = Field(default="", description="本次状态的写入时间（UTC ISO 8601）")
    view: dict[str, Any] = Field(
        default_factory=dict, description="本次重建后的视图摘要（enabled_skills / skipped）"
    )


__all__ = [
    "AttachmentDeleteResponse",
    "ChatRequest",
    "DecisionPayload",
    "DeleteResponse",
    "EditedAction",
    "HealthResponse",
    "HistoryMessage",
    "KnowledgeCapabilities",
    "KnowledgeDeleteResponse",
    "KnowledgeDocumentInfo",
    "KnowledgeIndexItem",
    "KnowledgeIndexRequest",
    "KnowledgeIndexResponse",
    "KnowledgeListResponse",
    "KnowledgeStats",
    "ModelInfo",
    "ResumeRequest",
    "SkillInfo",
    "SkillListResponse",
    "SkillToggleRequest",
    "SkillToggleResponse",
    "SkillUnloadable",
    "StopResponse",
    "ThreadListResponse",
    "ThreadResponse",
    "ThreadUpdateRequest",
]
