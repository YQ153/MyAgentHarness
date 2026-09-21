"""应用层对外暴露的基础设施端口。

WHY 需要端口：``interfaces`` 层需要操作审计日志与运行状态，
但不应依赖 ``runtime`` 的具体实现类——那会让「更换存储实现」波及接口层，
也破坏 ``interfaces → application`` 的单向依赖。

用 ``typing.Protocol`` 描述所需能力后：

- ``interfaces`` 只依赖 ``application``，依赖方向合规；
- ``runtime`` 的实现类因结构化子类型（structural subtyping）自动满足协议，
  无需显式继承，也无需反向导入本模块，因此不产生新的耦合。

第二类端口（2026-09-20 追加）服务的是**应用层自身**：``ThreadMetadata*`` /
``AuditLog`` / ``UsageLedger`` / ``KnowledgeIndex`` / ``SkillState``。
理由与上面完全相同，只是此前只想到了接口层那一侧——应用层的服务同样直接
依赖 ``runtime`` 的实现类，于是「更换存储实现」仍然会波及应用层自己。

WHY 只端口化「有状态、变更理由随存储技术走」的实现类：无状态的函数模块
（``workspace_files`` 的路径校验、``tool_outputs`` 的路径换算、``attachments``
的读写函数）不在其列——它们没有「另一种实现」的诉求；而 ``workspace_files``
是路径校验这一安全边界的唯一实现，为它引入协议只会诱发第二份实现。
数据载体（``ChunkInput`` / ``KnowledgeHit``）同理保留直接依赖：它们的形状由
两侧共同理解，与存储技术无关。

> 单元测试可用任意替身满足这些协议（结构化子类型，无需继承）；
> ``tests/application/test_runtime_port_contract.py`` 守住「哪些直接依赖是
> 有意保留的」。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

# 数据载体：端口签名里必须出现它们（KnowledgeStore 的产出与入参），
# 而它们由 runtime 定义——契约 3 禁止 runtime 反向依赖 application，
# 因此这两者只能来自 runtime。它们不是「可替换的实现」，见模块 docstring。
from runtime.knowledge_store import ChunkInput, KnowledgeHit


class AuditSink(Protocol):
    """审计事件的写入与查询能力。"""

    async def log(
        self,
        *,
        event_type: str,
        actor_id: str,
        target_id: str | None = None,
        action: str | None = None,
        outcome: str,
        ip: str | None = None,
        user_agent: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """记录一条审计事件。"""
        ...

    async def list(
        self,
        *,
        actor_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按时间倒序列出审计事件。"""
        ...


# ---------------------------------------------------------------------- 会话元数据
#
# 拆成读 / 写 / 分支三个窄协议，而不是照抄 ThreadMetaStore：
# 对象只用到其中一部分能力时，注解应当如实反映这一点——AttachmentService 与
# HealthService 只读会话元数据，让它们被迫接受写入能力就是在放宽依赖。
#
# 方法签名逐条照抄 ``runtime/thread_store.py`` 的实现（不臆造）。这是有意为之：
# 协议与实现的签名一旦分叉，类型检查器会指向一个「合法的」错误位置，
# 而真正的修法在实现里。


class ThreadMetadataReader(Protocol):
    """会话元数据的读取能力。"""

    async def get(self, thread_id: str) -> dict[str, Any] | None:
        """按 ID 读取会话元数据；不存在时返回 ``None``。"""
        ...

    async def list_threads(
        self,
        *,
        owner_id: str | None = None,
        include_unowned: bool = False,
        limit: int = 50,
        offset: int = 0,
        query: str | None = None,
        tag: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """按条件列出会话元数据。"""
        ...

    async def count(
        self,
        *,
        owner_id: str | None = None,
        include_unowned: bool = False,
        query: str | None = None,
        tag: str | None = None,
        include_archived: bool = False,
    ) -> int:
        """统计满足条件的会话数。"""
        ...

    async def ping(self) -> bool:
        """探测存储连通性；用于就绪检查。"""
        ...


class ThreadMetadataWriter(Protocol):
    """会话元数据的写入能力。"""

    async def record_turn(
        self,
        thread_id: str,
        *,
        title_hint: str | None = None,
        turn_delta: int = 1,
        owner_id: str = "",
    ) -> dict[str, Any] | None:
        """记录一轮对话，并返回更新后的元数据；会话不存在时返回 ``None``。"""
        ...

    async def touch(self, thread_id: str) -> bool:
        """刷新会话的最近活动时间。"""
        ...

    async def rename(self, thread_id: str, title: str) -> dict[str, Any] | None:
        """重命名会话。"""
        ...

    async def set_archived(self, thread_id: str, archived: bool) -> dict[str, Any] | None:
        """归档或取消归档会话。"""
        ...

    async def set_tags(self, thread_id: str, tags: list[str] | None) -> dict[str, Any] | None:
        """整体替换会话标签。"""
        ...

    async def delete(self, thread_id: str) -> bool:
        """删除会话元数据；返回是否实际删除了记录。"""
        ...


class ThreadBranchStore(Protocol):
    """会话分支记录的读写能力。"""

    async def current_branch(self, thread_id: str) -> str:
        """返回当前激活的分支标识；未记录时为空串（根分支）。"""
        ...

    async def list_branches(self, thread_id: str) -> list[dict[str, Any]]:
        """列出会话的全部分支。"""
        ...

    async def get_branch(self, thread_id: str, branch_id: str) -> dict[str, Any] | None:
        """读取单条分支记录；不存在时返回 ``None``。"""
        ...

    async def set_branch_head(
        self, thread_id: str, branch_id: str, head_checkpoint: str
    ) -> None:
        """冻结某分支的头检查点。"""
        ...

    async def set_current_branch(self, thread_id: str, branch_id: str) -> bool:
        """切换当前激活的分支。"""
        ...

    async def upsert_branch(
        self,
        thread_id: str,
        branch_id: str,
        *,
        parent_branch_id: str = "",
        origin: str = "",
        label: str = "",
    ) -> dict[str, Any]:
        """登记（或更新）一条分支记录。"""
        ...


class ThreadMetadataStore(
    ThreadMetadataReader,
    ThreadMetadataWriter,
    ThreadBranchStore,
    Protocol,
):
    """会话元数据的完整能力（读 + 写 + 分支）。

    WHY 组合而不是让服务标注三个协议：构造参数只能有一个类型注解，
    而 ``ThreadService`` / ``RunService`` 确实同时用到三类能力。
    组合协议让它们保持「会话元数据存储」这一个概念，
    同时不影响只用其中一部分的服务标注更窄的协议。
    """


# ---------------------------------------------------------------------- 审计


class AuditLog(Protocol):
    """审计事件的写入与计数能力。

    与 :class:`AuditSink` 的关系：这是它的超集（多一个 ``count_all``）。
    接口层继续用窄的 ``AuditSink``（它只写不查），应用层用这个。
    """

    async def log(
        self,
        *,
        event_type: str,
        actor_id: str,
        target_id: str | None = None,
        action: str | None = None,
        outcome: str,
        ip: str | None = None,
        user_agent: str | None = None,
        trace_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """记录一条审计事件。"""
        ...

    async def count_all(self) -> int:
        """返回审计事件总数；用于运行指标。"""
        ...


# ---------------------------------------------------------------------- 用量


class UsageLedger(Protocol):
    """Token 用量的落库与聚合能力。"""

    async def record(
        self,
        *,
        thread_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        owner_id: str = "",
        trace_id: str | None = None,
        created_at: str | None = None,
    ) -> int:
        """记录一次运行的用量；返回新记录的 ID。"""
        ...

    async def summarize(
        self,
        *,
        owner_id: str | None = None,
        thread_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        group_by: str = "model",
    ) -> dict[str, Any]:
        """按维度聚合时间窗内的用量。"""
        ...


# ---------------------------------------------------------------------- 知识库


class KnowledgeIndex(Protocol):
    """工作区文档索引的读写与检索能力。

    ``dims`` / ``model`` / ``vector_enabled`` 是实例属性而非方法
    （实现里就是普通属性），因此这里声明为协议属性。
    """

    dims: int
    """向量维度。"""

    model: str
    """嵌入模型标识；未启用嵌入时为空串。"""

    vector_enabled: bool
    """是否具备向量检索能力。"""

    async def get_document(self, *, owner_id: str, source_path: str) -> dict[str, Any] | None:
        """按来源路径读取文档索引记录。"""
        ...

    async def replace_document(
        self,
        *,
        owner_id: str,
        source_path: str,
        content_hash: str,
        chunks: Sequence[ChunkInput],
        vectors: Sequence[Sequence[float]] | None = None,
    ) -> dict[str, Any]:
        """整体替换一份文档的分块与向量。"""
        ...

    async def delete_document(self, *, owner_id: str, source_path: str) -> bool:
        """删除一份文档的全部索引。"""
        ...

    async def list_documents(self, *, owner_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """列出某主体的已索引文档。"""
        ...

    async def stats(self, *, owner_id: str) -> dict[str, Any]:
        """返回索引规模统计。"""
        ...

    async def search_keyword(
        self, *, owner_id: str, query: str, limit: int = 10
    ) -> list[KnowledgeHit]:
        """关键词检索。"""
        ...

    async def search_vector(
        self, *, owner_id: str, vector: Sequence[float], limit: int = 10
    ) -> list[KnowledgeHit]:
        """向量检索；未启用向量能力时实现会抛错。"""
        ...


# ---------------------------------------------------------------------- 技能


class SkillState(Protocol):
    """技能启停状态的读写能力。"""

    async def set_enabled(
        self, skill_name: str, enabled: bool, *, scope: str = "global"
    ) -> dict[str, Any]:
        """设置某技能的启停状态。"""
        ...

    async def resolve(
        self, skill_names: list[str], *, scope: str = "global"
    ) -> dict[str, bool]:
        """批量解析技能的生效状态。"""
        ...


__all__ = [
    "AuditLog",
    "AuditSink",
    "KnowledgeIndex",
    "SkillState",
    "ThreadBranchStore",
    "ThreadMetadataReader",
    "ThreadMetadataStore",
    "ThreadMetadataWriter",
    "UsageLedger",
]
