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
    supports_vision: bool = Field(
        default=False, description="是否接受图片输入；前端据此决定能否上传附件"
    )


class AttachmentInfo(BaseModel):
    """一个已上传的附件。"""

    id: str = Field(description="附件标识，发起运行时用它引用")
    thread_id: str = Field(description="所属会话")
    filename: str = Field(description="上传时的原始文件名，仅用于展示")
    mime_type: str = Field(description="内容类型")
    size: int = Field(description="字节数")
    sha256: str = Field(description="内容摘要，用于审计与去重比对")
    created_at: str = Field(description="上传时刻（ISO8601 UTC）")
    path: str = Field(description="工作区内的虚拟路径，前端可直接交给文件接口预览")


class AttachmentLimits(BaseModel):
    """附件相关的上限，随清单一起下发。

    WHY 与清单同一个响应：前端要在**用户选文件的那一刻**就给出「太大 / 类型不支持」
    的提示，而不是等上传请求失败。让它多打一次接口只会多一条「配置与提示不一致」
    的分叉路径。
    """

    max_bytes: int = Field(description="单个附件的字节上限")
    max_per_thread: int = Field(description="单会话附件数上限")
    allowed_mime_types: list[str] = Field(description="允许上传的 MIME 白名单")


class AttachmentListResult(BaseModel):
    """某会话的附件清单及其上限。"""

    thread_id: str = Field(description="会话标识")
    items: list[AttachmentInfo] = Field(default_factory=list, description="按上传时间升序")
    limits: AttachmentLimits = Field(description="当前生效的上限")


class WorkspaceEntryInfo(BaseModel):
    """文件面板里的一条目录项。"""

    name: str = Field(description="条目名")
    path: str = Field(description="虚拟路径，可直接回传给文件接口")
    is_dir: bool = Field(description="是否为目录")
    size: int = Field(description="字节数")
    modified_at: float = Field(description="最近修改时间（Unix 秒）")
    is_symlink: bool = Field(default=False, description="是否为符号链接")


class WorkspaceListing(BaseModel):
    """一次目录列举的结果。"""

    path: str = Field(description="当前目录的虚拟路径")
    parent: str | None = Field(default=None, description="上级目录虚拟路径；根目录为 None")
    entries: list[WorkspaceEntryInfo] = Field(default_factory=list, description="目录项，目录在前")
    truncated: bool = Field(default=False, description="是否因条目数上限被截断")


class WorkspaceFileContent(BaseModel):
    """一次文件读取的结果。

    WHY 用 ``kind`` 而不是让调用方看状态码区分：``binary`` / ``too_large`` /
    ``image`` 都是「读取成功但展示方式不同」，与读取失败（400 / 404）走的是
    完全不同的分支——把前者做成错误，界面就只能显示一句失败，而用户想知道的是
    「多大的什么文件」。
    """

    path: str = Field(description="虚拟路径")
    name: str = Field(description="文件名")
    size: int = Field(description="文件总字节数")
    kind: str = Field(description="text / image / binary / too_large")
    text: str = Field(default="", description="本次返回的文本片段，或图片的 data URL；其余类型为空")
    truncated: bool = Field(
        default=False, description="本次返回的片段之后是否还有内容（前端据此提供「加载更多」）"
    )
    offset: int = Field(default=0, description="本次片段在文件中的起始字符偏移；续取时原样传回")
    mime_type: str = Field(default="", description="据扩展名推断的 MIME 类型")


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
    tags: list[str] = Field(default_factory=list, description="会话标签，按写入顺序排列")
    archived: bool = Field(default=False, description="是否已归档（软删除）")
    archived_at: str = Field(default="", description="归档时刻（ISO8601 UTC）；未归档为空串")


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
    tool_call_id: str = Field(
        default="",
        description="工具消息对应的调用标识；非工具消息为空串。导出后要能原样导回，缺了它工具消息无法重建",
    )
    attachments: list[AttachmentInfo] = Field(
        default_factory=list,
        description="该消息携带的图片附件；正文只保留文字，图片内容在附件目录里独立存放",
    )


EXPORT_VERSION = "1"
"""会话导出格式的版本号。

WHY 单独列出来并写进文件：导入是「别人给我的文件」，格式一旦变化，没有版本号就
只能靠字段猜测兼容性，而猜错的表现是内容静默错位。
"""


class ThreadExport(BaseModel):
    """一个会话的可移植快照。

    WHY 只导出当前分支的消息：分支结构与各分支的位置是**上游检查点**的概念，
    导出成文件后无法在目标库里重建出同样的树；把某一分支的内容原样带过去，
    比带一个看起来有结构、实际无法复原的树要诚实。
    """

    version: str = Field(default=EXPORT_VERSION, description="格式版本")
    thread_id: str = Field(description="来源会话标识；导入时会分配新 ID")
    title: str = Field(default="", description="会话标题")
    tags: list[str] = Field(default_factory=list, description="会话标签")
    created_at: str = Field(default="", description="来源会话创建时间（ISO8601 UTC）")
    updated_at: str = Field(default="", description="来源会话最近活动时间（ISO8601 UTC）")
    exported_at: str = Field(default="", description="导出时刻（ISO8601 UTC）")
    branch_id: str = Field(default="", description="导出的是哪条分支；空串表示根分支")
    messages: list[HistoryMessage] = Field(default_factory=list, description="该分支的消息")
    notes: list[str] = Field(
        default_factory=list, description="导入方需要知道的事项，随文件一起传给对方"
    )


class ImportResult(BaseModel):
    """导入结果。"""

    thread_id: str = Field(description="新建的会话标识")
    title: str = Field(default="", description="新会话的标题")
    message_count: int = Field(default=0, description="复原的消息条数")
    skipped_messages: int = Field(default=0, description="因角色无法还原而丢弃的条数")
    usage_note: str = Field(
        default="", description="用量口径说明：新会话的用量自导入时刻重新计"
    )
    notes: list[str] = Field(default_factory=list, description="随导出文件带回的注意事项")


class BranchSummary(BaseModel):
    """会话的一条分支。

    WHY ``head_checkpoint`` 对当前分支为空串：当前分支的头随每次运行前移，存下来
    必然过期；而它本来就是「会话当前的头」，读的时候现取即可。存下的是**离开**该分支
    时冻结的那个头——那才是切回来时需要的东西。
    """

    branch_id: str = Field(description="分支标识，会话内唯一；空串表示根分支")
    head_checkpoint: str = Field(description="离开该分支时冻结的检查点 id；当前分支为空串")
    parent_branch_id: str = Field(description="从哪条分支分叉而来；根分支为空串")
    origin: str = Field(description="分支来源：root / edit / regenerate")
    label: str = Field(description="给界面看的简短说明，如「编辑第 2 轮」")
    created_at: str = Field(description="创建时间（ISO8601 UTC）")
    current: bool = Field(default=False, description="是否为当前激活的分支")


class BranchListResult(BaseModel):
    """会话的分支清单。"""

    thread_id: str = Field(description="会话标识")
    current_branch: str = Field(description="当前分支标识；空串表示根分支")
    items: list[BranchSummary] = Field(default_factory=list, description="按创建时间升序排列")


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


class UsageGroup(BaseModel):
    """按某一维度聚合出的一档用量。"""

    key: str = Field(description="分组键：模型别名 / 会话 ID / 日期（YYYY-MM-DD）")
    prompt_tokens: int = Field(description="输入 token 合计")
    completion_tokens: int = Field(description="输出 token 合计")
    total_tokens: int = Field(description="输入输出合计")
    run_count: int = Field(description="该分组内的运行次数")


class UsageSummary(BaseModel):
    """一个时间窗内的用量汇总。

    WHY 同时给出总计与分组：总计回答「这段时间花了多少」，分组回答
    「花在哪个模型 / 哪个会话 / 哪一天」。只给前者无法定位，只给后者
    需要调用方自己再算一遍总和。
    """

    window_days: int = Field(description="统计窗口天数")
    since: str = Field(description="窗口起点（ISO8601 UTC，含）")
    group_by: str = Field(description="分组维度：model / thread / day")
    thread_id: str | None = Field(default=None, description="限定会话时为其 ID，否则为 None")
    prompt_tokens: int = Field(description="输入 token 合计")
    completion_tokens: int = Field(description="输出 token 合计")
    total_tokens: int = Field(description="输入输出合计")
    run_count: int = Field(description="运行次数")
    groups: list[UsageGroup] = Field(description="按维度聚合的分档明细")


class GovernanceReport(BaseModel):
    """一次运行治理巡检的结果。

    WHY 用模型而非裸计数：巡检是后台协程调用，结果只服务于日志与测试断言，
    但它同样是一份「服务层对外契约」——字段增删若只体现为字典键的变化，
    调用方（指标、测试）会在运行期才炸，而这里能让类型检查提前拦住。
    """

    timed_out_runs: int = Field(default=0, description="本轮巡检中被判定超时并强制取消的运行数")
    expired_hitl: int = Field(default=0, description="本轮巡检中被判定超期并作废的审批挂起数")
    checked_runs: int = Field(default=0, description="本轮巡检看到的运行中会话数")
    checked_hitl: int = Field(default=0, description="本轮巡检看到的挂起审批数")


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
    timed_out_runs: int = Field(
        default=0, description="进程启动以来被运行超时强制取消的运行次数"
    )
    expired_hitl: int = Field(
        default=0, description="进程启动以来因超期未决策而作废的审批挂起次数"
    )
    max_concurrent_runs: int = Field(default=0, description="配置的全局并发上限；0 表示不限制")
    available_run_slots: int = Field(
        default=-1,
        description="当前可用的运行槽位数；上限为 0（不限制）时以 -1 表示",
    )
    rejected_runs: int = Field(
        default=0, description="进程启动以来因超出并发上限或被限流而拒绝的运行次数"
    )
    audit_events: int | None = Field(
        default=None, description="审计事件总数；``None`` 表示本次采集失败"
    )
    uptime_seconds: float = Field(description="进程已运行秒数")


class ToolInfo(BaseModel):
    """一个对模型可见的工具。

    WHY 把内置工具一并列出：运维要回答的是「这个 Agent 究竟能做什么」，
    只列扩展工具会让人误以为内置的文件与执行能力不存在。
    """

    name: str = Field(description="工具名，模型调用时使用")
    source: str = Field(description="来源：builtin / custom / mcp")
    description: str = Field(default="", description="工具描述；内置工具无描述时为空串")
    server: str | None = Field(
        default=None, description="来源 MCP 服务器名；非 MCP 工具为 None"
    )


class MCPServerInfo(BaseModel):
    """一台 MCP 服务器的加载结果。"""

    name: str = Field(description="服务器名")
    transport: str = Field(description="传输方式：stdio / sse / streamable_http / websocket")
    ok: bool = Field(description="工具清单是否加载成功")
    tool_count: int = Field(default=0, description="该服务器提供的工具数")
    error: str = Field(default="", description="失败原因摘要；成功时为空串")


class ToolListResult(BaseModel):
    """工具清单及其来源构成。"""

    items: list[ToolInfo] = Field(description="全部生效工具，内置在前、扩展在后")
    total: int = Field(description="工具总数")
    custom_modules: list[str] = Field(description="已加载的自定义工具模块")
    mcp_servers: list[MCPServerInfo] = Field(description="已配置的 MCP 服务器及其状态")


class MemoryItem(BaseModel):
    """一条长期记忆。

    WHY 直接返回正文而不是只给路径：面板要回答的是「Agent 究竟记住了我什么」，
    只列路径会把自查变成逐条点开；而记忆的内容本来就是用户自己产生的数据，
    不存在「读了不该读的」这一层风险。
    """

    path: str = Field(description="记忆路径，形如 /memories/prefs.md")
    content: str = Field(description="记忆正文；过长时被截断")
    created_at: str = Field(default="", description="首次写入时间（ISO8601 UTC）")
    updated_at: str = Field(default="", description="最近修改时间（ISO8601 UTC）")
    truncated: bool = Field(default=False, description="正文是否被截断")


class MemoryListResult(BaseModel):
    """某主体的长期记忆清单。

    WHY 带 ``truncated`` 标志：记忆条数或单条正文都可能被上限截断，而截断后
    的清单与「记忆本来就这么少」在界面上无法区分——那会直接演变成
    「我的记忆丢了」这类误报。
    """

    owner_id: str = Field(description="清单归属主体")
    items: list[MemoryItem] = Field(description="按路径排序的记忆条目")
    total: int = Field(description="本次返回的条目数")
    truncated: bool = Field(default=False, description="是否因上限截断了条目或正文")


class MemoryDeleteResult(BaseModel):
    """删除一条长期记忆的结果。"""

    path: str = Field(description="被删除的记忆路径")
    deleted: bool = Field(description="是否确实移除了条目；False 表示该路径本就不存在")
