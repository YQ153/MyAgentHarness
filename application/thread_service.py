"""会话服务：会话标识、清单、历史与删除。

职责边界：只编排「会话」这一聚合的读写，不涉及运行期事件流
（见 ``application.run_service.RunService``）。两者此前合并在一个类里，
导致任何一处改动都要牵动另一处的依赖，也让单元测试必须同时构造图、
检查点与元数据存储。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from application.attachment_service import attachment_info
from application.audit_context import LOCAL_ACTOR_ID, audit_client_info, audit_trace_id
from application.dto import (
    EXPORT_VERSION,
    AttachmentInfo,
    BranchListResult,
    BranchSummary,
    DeleteOutcome,
    DeleteResult,
    HistoryMessage,
    ImportResult,
    ThreadExport,
    ThreadListResult,
    ThreadSummary,
)
from application.errors import NotFoundError
from application.ports import AuditLog, ThreadMetadataStore
from application.runnable import build_runnable_config
from runtime.attachments import (
    AttachmentRecord,
    delete_thread_attachments,
    index_by_sha256,
)
from runtime.thread_store import normalize_search_query
from text_utils import collapse_whitespace
from thread_utils import normalize_thread_id

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from application.session_registry import SessionRegistry

    from agent.graph import AgentFactory
    from config import AppConfig

logger = logging.getLogger(__name__)


def _restore_messages(messages: list[HistoryMessage]) -> tuple[list[Any], int]:
    """把导出的消息还原成 LangChain 消息，返回（消息列表, 丢弃条数）。

    WHY 丢弃无法还原的角色而不是硬塞：图只会产生 human / ai / tool 三类。把未知角色
    当作用户消息塞进去，会静默改变模型看到的前缀——那比少一条消息更糟；而少掉多少条
    必须报给用户，所以这里返回计数而不是悄悄跳过。

    WHY 缺 call id 时补一个：上游要求每次工具调用都有 id，而手写的导出文件未必带。
    真正需要它的是「工具结果指向哪次调用」，那种对应关系由 tool_call_id 决定；
    缺失时宁可丢弃该条工具消息并计数，也不给它安一个对不上的 id。
    """
    restored: list[Any] = []
    skipped = 0

    for index, message in enumerate(messages or []):
        role = (message.role or "").lower()
        if role in {"human", "user"}:
            restored.append(HumanMessage(content=message.content))
        elif role in {"ai", "assistant"}:
            restored.append(
                AIMessage(
                    content=message.content,
                    tool_calls=[
                        {
                            "id": str(call.get("id") or f"call_{index}_{position}"),
                            "name": str(call.get("name") or ""),
                            "args": call.get("args") or {},
                        }
                        for position, call in enumerate(message.tool_calls or [])
                    ],
                )
            )
        elif role == "tool":
            if not message.tool_call_id:
                skipped += 1
                continue
            restored.append(
                ToolMessage(
                    content=message.content,
                    tool_call_id=message.tool_call_id,
                    name=message.name or None,
                )
            )
        else:
            skipped += 1

    return restored, skipped


class ThreadService:
    """管理会话的元数据生命周期。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        checkpointer: BaseCheckpointSaver,
        thread_store: ThreadMetadataStore,
        graph_factory: AgentFactory,
        workspaces: SessionRegistry,
        audit_store: AuditLog | None = None,
    ) -> None:
        """构造会话服务。

        Args:
            config: 应用配置。
            checkpointer: 检查点保存器，用于清理会话状态。
            thread_store: 会话元数据存储。
            graph_factory: 图工厂，用于读取会话历史。
            workspaces: 会话级工作区的解析入口；**必填**——附件索引与清理都要按
                **该会话**的工作区来做，沿用进程默认值会去扫别人的目录。
            audit_store: 审计日志存储，可选；``None`` 时不记录审计。

        Raises:
            ValueError: 任一必需依赖为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if checkpointer is None:
            # WHY 强制注入而非内部自建：异步检查点的连接由上下文管理器托管，
            # 本类若自行创建就无从关闭，必然泄漏连接。
            raise ValueError("checkpointer 不能为 None")
        if thread_store is None:
            raise ValueError("thread_store 不能为 None")
        if graph_factory is None:
            raise ValueError("graph_factory 不能为 None")
        if workspaces is None:
            raise ValueError("workspaces 不能为 None：附件索引按会话的工作区解析")

        self._config = config
        self._checkpointer = checkpointer
        self._thread_store = thread_store
        self._graph_factory = graph_factory
        self._workspaces = workspaces
        self._audit_store = audit_store

        logger.info("ThreadService 就绪：sessions_root=%s", config.resolved_sessions_root)

    async def _require_record(self, thread_id: str) -> dict[str, Any]:
        """读取会话元数据；会话不存在时抛 ``NotFoundError``。

        WHY 统一收在这里：所有面向单条会话的读写都要先确认它存在，各方法各写一次
        必然出现「有的操作报 404、有的静默返回空结果」的漂移——而后者会让调用方
        把「会话不存在」当成「这条会话什么都没有」。

        Raises:
            NotFoundError: 该会话在元数据表里不存在。
        """
        record = await self._thread_store.get(thread_id)
        if record is None:
            raise NotFoundError("会话", thread_id)
        return record

    async def _audit(
        self,
        *,
        event_type: str,
        actor_id: str,
        target_id: str | None = None,
        action: str | None = None,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """记录一条审计事件，并自动补上当前请求的 IP / User-Agent / 链路标识。

        WHY 三者由本方法统一补齐：调用点只关心「记了什么」，来源信息由接口层中间件
        写入的 ``contextvars`` 提供，任一调用点都不会漏；审计失败同样不上抛——
        它是旁路职责，不应把一次成功的删除变成 500。
        """
        if self._audit_store is None:
            return
        ip, ua = audit_client_info()
        try:
            await self._audit_store.log(
                event_type=event_type,
                actor_id=actor_id,
                target_id=target_id,
                action=action,
                outcome=outcome,
                ip=ip,
                user_agent=ua,
                # WHY 与 IP/UA 同一处读取：三者都是「这次请求的来路」，只补其中一个
                # 会让会话侧的审计记录在链路视图里断掉——那正是排查时最需要连贯的一段。
                trace_id=audit_trace_id(),
                details=details,
            )
        except Exception:
            logger.exception("审计事件写入失败：event_type=%s actor=%s", event_type, actor_id)

    # ------------------------------------------------------------------ 查询

    def new_thread_id(self) -> str:
        """申请一个新的会话 ID。

        WHY 只发号、不落库：会话的诞生时刻被定义为「首条用户消息被接受」
        （见 ``RunService.stream``）。若在此处登记元数据，前端每次刷新页面都会
        留下一行既无标题也无消息的空会话，CLI 启动后直接退出同样如此——空会话
        没有任何信息价值，却会永久占据会话清单。

        WHY 仍由服务端发号而不交给前端生成：会话 ID 是写入检查点的键，
        由服务端统一生成才能保证格式与唯一性口径只有一处；``uuid4`` 不可预测，
        客户端也无法据此猜测其它会话的 ID。

        Returns:
            新会话的 ID；此刻数据库中尚未产生任何记录。
        """
        return uuid.uuid4().hex

    async def list_threads(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        query: str | None = None,
        tag: str | None = None,
        include_archived: bool = False,
    ) -> ThreadListResult:
        """列出会话清单，最近活动的在前。

        WHY 只读元数据表而不遍历每个会话的图状态：后者需要对每个 thread 调用
        一次 ``aget_state``，成本随会话数线性增长；而列表页只需要标题与时间。

        Args:
            limit: 返回条数，1..200。
            offset: 跳过的条数，用于分页。
            query: 标题关键字；``None`` 表示不过滤。搜索只覆盖标题——正文检索
                需要反序列化检查点，属另一个量级的工作，本层不承担。
            include_archived: 是否连同已归档的会话一起返回。

        Returns:
            会话清单与总数。

        Raises:
            ValueError: 分页参数或关键字非法（调用方应映射为 400）。
            RuntimeError: 查询失败（调用方应映射为 500）。
        """
        # WHY 在服务层也校验一次关键字：依赖「存储实现恰好会校验」是不可靠的——
        # 换一个存储实现，非法关键字就会从 400 变成 500 或静默全表匹配。
        normalized_query = normalize_search_query(query)
        try:
            # WHY 两个查询传完全相同的过滤条件：总数与实际返回条数必须对得上，
            # 否则前端会显示「还有下一页」但翻过去是空的。
            items = await self._thread_store.list_threads(
                limit=limit,
                offset=offset,
                query=normalized_query,
                tag=tag,
                include_archived=include_archived,
            )
            total = await self._thread_store.count(
                query=normalized_query,
                tag=tag,
                include_archived=include_archived,
            )
        except ValueError:
            # WHY 让参数错误原样透出：路由层需要把它映射为 400 而不是 500
            raise
        except Exception as exc:
            logger.exception("查询会话列表失败：limit=%s offset=%s query=%s", limit, offset, query)
            raise RuntimeError("查询会话列表失败") from exc

        logger.debug("会话列表返回 %d 条，总计 %d 条", len(items), total)
        return ThreadListResult(
            items=[ThreadSummary(**item) for item in items],
            total=total,
        )

    async def history(
        self,
        thread_id: str,
        *,
        branch_id: str | None = None,
    ) -> list[HistoryMessage]:
        """读取会话历史，供前端刷新页面后恢复上下文。

        WHY 用 ``aget_state`` 而不用同步的 ``get_state``：异步检查点保存器
        只实现了异步接口，调用同步方法会抛 ``NotImplementedError``。

        WHY 只在真正查不到状态时返回空列表，而不再吞掉异常：此前所有异常都降级
        成 ``[]``，会把「数据库不可用」伪装成「这个会话没有历史」，前端与调用方
        都无法区分，故障也就不可观测。``aget_state`` 对不存在的会话返回的是空
        状态快照而不是异常，因此空列表仍然精确对应「无历史」。

        WHY 支持指定分支：分叉之后「会话最新状态」与「用户正在看的那条分支」不再是
        同一件事。整条分支切换路径之所以便宜，是因为它只是换个检查点 id 去读同一个
        接口——上游本来就支持按 id 取任意历史点的状态。

        Args:
            thread_id: 会话 ID。
            branch_id: 要读取的分支；``None`` 表示当前分支。

        Returns:
            该分支的历史消息列表；会话不存在时为空列表。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话元数据或指定分支不存在。
            RuntimeError: 读取失败。
        """
        normalized = normalize_thread_id(thread_id)
        record = await self._require_record(normalized)

        # WHY 按会话的工作区取图与建附件索引：历史消息里的附件引用是「相对本会话工作区」
        # 的相对路径，拿另一个根去索引只会得到一份空映射——表现为「历史里的图全丢了」，
        # 而文件其实还在原处。
        scope = await self._workspaces.resolve(thread_id=normalized, record=record)
        graph = self._graph_factory.get(scope=scope)
        checkpoint = await self._resolve_branch(normalized, branch_id)

        try:
            state = await graph.aget_state(
                build_runnable_config(self._config, normalized, checkpoint)
            )
        except Exception as exc:
            logger.exception("读取会话历史失败：thread=%s", normalized)
            raise RuntimeError(f"读取会话历史失败：thread={normalized}") from exc

        if state is None:
            return []

        messages = getattr(state, "values", {}).get("messages") or []
        # WHY 先建一次索引而不是每条消息各自查一次：附件目录的列举要扫目录，
        # 一条消息查一次会把一次历史读取放大成 N 次目录扫描。
        attachment_index = await asyncio.to_thread(index_by_sha256, scope.root, normalized)
        return [self._message_to_dto(message, attachment_index) for message in messages]

    # ------------------------------------------------------------------ 分支

    async def list_branches(
        self,
        thread_id: str,
    ) -> BranchListResult:
        """列出会话的全部分支，并标出当前分支。

        WHY 把「当前分支」一并返回：界面要据此高亮，而它本来就是会话行上的一列——
        让调用方逐个分支去问一次既慢又容易与切换结果不一致。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话元数据不存在。
        """
        normalized = normalize_thread_id(thread_id)
        record = await self._require_record(normalized)

        current = await self._thread_store.current_branch(normalized)
        rows = await self._thread_store.list_branches(normalized)
        items = [self._branch_to_dto(row, current) for row in rows]

        # WHY 补一条根分支：从未分叉过的会话在分支表里没有任何行，而「当前分支」此时
        # 正是空串（根分支）——不补的话，界面上被标为当前的那条分支根本不在清单里。
        if not any(item.branch_id == "" for item in items):
            items.insert(
                0,
                BranchSummary(
                    branch_id="",
                    head_checkpoint="",
                    parent_branch_id="",
                    origin="root",
                    label="原始分支",
                    created_at=(record or {}).get("created_at", ""),
                    current=current == "",
                ),
            )

        return BranchListResult(thread_id=normalized, current_branch=current, items=items)

    async def activate_branch(
        self,
        thread_id: str,
        branch_id: str,
    ) -> BranchListResult:
        """把某条分支设为当前分支。

        WHY 切换前必须冻结原来的当前分支：它的头随每次运行前移，一旦不再是当前分支就
        没有任何地方能说明它停在哪——不冻结的话，它下次被切回来会接到会话最新状态上，
        也就是接错分支。这正是「分叉之后必须自己记分支头」的直接后果。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话或指定分支不存在。
        """
        normalized = normalize_thread_id(thread_id)
        record = await self._require_record(normalized)

        if branch_id != "":
            target = await self._thread_store.get_branch(normalized, branch_id)
            if target is None:
                raise NotFoundError("分支", branch_id)

        current = await self._thread_store.current_branch(normalized)
        if current != branch_id:
            # WHY 不无条件冻结原分支：此刻会话头属于**最新**那条分支，未必是正要离开
            # 的这一条。只在它「从未被离开过」时补记一次——那种情况下会话头正是它自己
            # 的头（详见 ``RunBranchService._freeze_unrecorded`` 的归纳）。
            existing = await self._thread_store.get_branch(normalized, current)
            if not (existing or {}).get("head_checkpoint"):
                head = await self._live_head(normalized)
                if head:
                    await self._thread_store.set_branch_head(normalized, current, head)
            await self._thread_store.set_current_branch(normalized, branch_id)
            await self._audit(
                event_type="thread_branch",
                actor_id=LOCAL_ACTOR_ID,
                target_id=normalized,
                action="activate",
                outcome="success",
                details={"branch_id": branch_id, "frozen_branch_id": current},
            )
            logger.info(
                "会话切换分支：thread=%s from=%s to=%s", normalized, current, branch_id
            )

        return await self.list_branches(normalized)

    async def _resolve_branch(self, thread_id: str, branch_id: str | None) -> str | None:
        """把分支标识解析成要读的检查点 id。

        WHY 以分支记录里的头为准：会话头永远指向最新那条分支，只有当前分支恰好是它时
        两者才相等。切回旧分支后若按会话头去读，旧分支会被显示成新分支的内容。

        Returns:
            检查点 id；``None`` 表示回落到会话当前的头（尚无记录的分支）。

        Raises:
            NotFoundError: 指定分支既不是当前分支、也没有记录在案。
        """
        record = await self._thread_store.get_branch(thread_id, branch_id or "")
        head = (record or {}).get("head_checkpoint") or ""
        if head:
            return head

        current = await self._thread_store.current_branch(thread_id)
        if branch_id is None or branch_id == current:
            return None

        raise NotFoundError("分支", branch_id)

    async def _live_head(self, thread_id: str) -> str:
        """读会话当前的头检查点 id；空会话或读取失败时返回空串。

        WHY 读取失败不回滚切换：切换本身只是改一列游标，代价可忽略；而因为读不到头
        就拒绝用户的切换，会让「检查点侧临时出问题」变成「分支功能不可用」。
        """
        graph = self._graph_factory.get(
            scope=await self._workspaces.resolve(thread_id=thread_id)
        )
        try:
            state = await graph.aget_state(
                build_runnable_config(self._config, thread_id)
            )
        except Exception:
            logger.exception("读取会话头检查点失败：thread=%s", thread_id)
            return ""

        config = getattr(state, "config", None) or {}
        return (config.get("configurable") or {}).get("checkpoint_id", "") or ""

    @staticmethod
    def _branch_to_dto(row: dict[str, Any], current: str) -> BranchSummary:
        """把分支行转成对外 DTO，并标出是否为当前分支。"""
        branch_id = str(row.get("branch_id") or "")
        return BranchSummary(
            branch_id=branch_id,
            head_checkpoint=str(row.get("head_checkpoint") or ""),
            parent_branch_id=str(row.get("parent_branch_id") or ""),
            origin=str(row.get("origin") or ""),
            label=str(row.get("label") or ""),
            created_at=str(row.get("created_at") or ""),
            current=branch_id == current,
        )

    # ------------------------------------------------------------------ 写入

    async def update_tags(
        self,
        thread_id: str,
        tags: list[str] | None,
    ) -> ThreadSummary:
        """整体替换会话标签。

        Raises:
            ValueError: ``thread_id`` 非法或标签不合法。
            NotFoundError: 会话不存在。
        """
        normalized = normalize_thread_id(thread_id)
        record = await self._require_record(normalized)

        updated = await self._thread_store.set_tags(normalized, tags)
        if updated is None:
            raise NotFoundError("会话", normalized)

        await self._audit(
            event_type="thread_tags",
            actor_id=LOCAL_ACTOR_ID,
            target_id=normalized,
            action="update",
            outcome="success",
            details={"tags": updated.get("tags") or []},
        )
        logger.info("会话标签已更新：thread=%s tags=%s", normalized, updated.get("tags"))
        return ThreadSummary(**updated)

    # ------------------------------------------------------------------ 导出与导入

    async def export_thread(
        self,
        thread_id: str,
        *,
        branch_id: str | None = None,
    ) -> ThreadExport:
        """把一个会话导出成可移植快照。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话或指定分支不存在。
        """
        normalized = normalize_thread_id(thread_id)
        record = await self._require_record(normalized)

        messages = await self.history(normalized, branch_id=branch_id)
        current = await self._thread_store.current_branch(normalized)

        return ThreadExport(
            version=EXPORT_VERSION,
            thread_id=normalized,
            title=str((record or {}).get("title") or ""),
            tags=[str(tag) for tag in ((record or {}).get("tags") or [])],
            created_at=str((record or {}).get("created_at") or ""),
            updated_at=str((record or {}).get("updated_at") or ""),
            exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            # WHY 记下实际导出的那条分支：缺省时导出的是当前分支，若这里留空，
            # 文件读者会以为导出的是根分支，拿它去对照界面就会对不上。
            branch_id=current if branch_id is None else branch_id,
            messages=messages,
            # WHY 导出带上工作区：消息正文里的工具输出引用是**工作区内**的虚拟路径
            # （形如 ``/_tool_outputs/...``）。不带来源目录，导入方会把这条会话的产物
            # 指向自己默认工作区里的同名路径——没有就 404，有就显示另一个项目的文件，
            # 两种都看不出「少了什么」。这里记的是**来源**，导入时是否沿用由导入方决定。
            workspace=str((record or {}).get("workspace") or ""),
            notes=[
                "只包含该分支的消息；分支结构与各分支的位置不随导出迁移。",
                "用量与审计不随导出迁移——导入后的会话自导入时刻重新计。",
                "工作区不随导出迁移：导入后的会话落在导入方的默认工作区，"
                "可用导入请求的 workspace 参数指定别处（文件里的 workspace 是来源取值，"
                "只在同一台机器上有意义）。",
            ],
        )

    async def import_thread(
        self,
        payload: ThreadExport,
        *,
        title: str | None = None,
        workspace: str | None = None,
    ) -> ImportResult:
        """把一份导出快照复原成一个**新会话**。

        WHY 永远新建、不提供覆盖：导出文件可能来自别人，覆盖语义意味着一个文件就能
        改写本机已有会话。新建让导入成为纯增量动作——失败也不会破坏任何现有数据。

        Raises:
            ValueError: 导入内容为空、版本不支持。
            RuntimeError: 写入检查点失败。
        """
        if payload is None:
            raise ValueError("导入内容不能为空")
        if str(payload.version) != EXPORT_VERSION:
            raise ValueError(f"不支持的导出格式版本：{payload.version}")

        new_id = uuid.uuid4().hex
        messages, skipped = _restore_messages(payload.messages)

        # WHY 导入也要定根：导入产生的是一个**新会话**，它此后就按这个根读写文件。
        # 取值由**导入方**给出（不给就用新会话的专属目录），而不是照搬导出文件里的
        # ``payload.workspace``——那是来源机器上的绝对路径，在本机通常不存在，照搬只会
        # 让导入失败。文件里的那个值仅作线索（``notes`` 里已说明）。
        # WHY 这里必须先把根定下来再登记：导入的会话带轮次，而「有轮次就有根」是本模型
        # 的一条不变式——漏了这一步，那条会话会成为一个永远解析不出根的黑洞。
        # WHY 传 ``thread_id=new_id``：让解析知道这是哪条会话，从而给出它自己的专属目录。
        scope = await self._workspaces.resolve(
            requested=workspace, thread_id=new_id, record={}, allow_missing=True
        )
        graph = self._graph_factory.get(scope=scope)
        try:
            # WHY 直接写入状态而不是「重放一遍对话」：重放会真的调模型——既慢，又会
            # 生出一轮与原文不同的回答。要的是复原，不是重新回答。
            # 全新会话上写入不需要 checkpoint_id / checkpoint_ns / as_node，
            # 这一点已由 scripts/probe_checkpoint_fork.py 的问题 6 验证过。
            await graph.aupdate_state(
                build_runnable_config(self._config, new_id),
                {"messages": messages},
            )
        except Exception as exc:
            logger.exception("导入写入检查点失败：thread=%s", new_id)
            raise RuntimeError(f"导入写入检查点失败：{exc}") from exc

        resolved_title = title or payload.title or "导入的会话"
        # WHY 用 record_turn 一步完成登记：它本身就是一条 UPSERT——刷新活动时间、
        # 累加轮次、标题为空时补写，并在需要时插入新行。前面再调一次 create 等于把
        # 同一件事做两遍，还多出一处「两遍之间失败」的中间态。
        # WHY 轮次按还原出的用户消息数补记：不补的话清单上会显示「0 轮」，而点进去
        # 有几十条消息——清单是用户判断「值不值得打开」的依据，不能与内容矛盾。
        await self._thread_store.record_turn(
            new_id,
            title_hint=resolved_title,
            turn_delta=sum(1 for message in messages if isinstance(message, HumanMessage)),
            workspace=str(scope.root),
            workspace_bound=bool(workspace and str(workspace).strip()),
        )
        if payload.tags:
            await self._thread_store.set_tags(new_id, payload.tags)
        await self._audit(
            event_type="thread_import",
            actor_id=LOCAL_ACTOR_ID,
            target_id=new_id,
            action="import",
            outcome="success",
            details={
                "message_count": len(messages),
                "skipped": skipped,
                "source_thread_id": payload.thread_id,
            },
        )

        return ImportResult(
            thread_id=new_id,
            title=resolved_title,
            message_count=len(messages),
            skipped_messages=skipped,
            usage_note="新会话的用量自导入时刻重新计；原会话的用量记录不随之迁移。",
            notes=list(payload.notes),
        )

    async def delete_thread(
        self,
        thread_id: str,
    ) -> DeleteResult:
        """删除会话：检查点与元数据一并清理。

        WHY 直接调用 checkpointer 的删除接口：图的状态读取无法区分「空会话」
        与「不存在」，而 ``update_state`` 只会追加写入，始终删不掉历史记录。

        WHY 返回结构化结果而非布尔值：``False`` 会同时表示「本来就不存在」
        「元数据删除失败」「检查点清理失败」三种含义，调用方无法判断该提示用户
        还是该重试。

        Args:
            thread_id: 会话 ID。

        Returns:
            删除结果，含结果分类、检查点是否清理与失败摘要。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话不存在。
        """
        normalized = normalize_thread_id(thread_id)

        record = await self._require_record(normalized)

        # WHY 在删元数据之前解析工作区：附件目录要按本会话绑定的根去找，而元数据
        # 一旦删掉，那个绑定就没了——之后再解析只能回落到默认工作区，于是「删了会话
        # 但附件留在原处」，且没有任何入口能再清掉它们。
        workspace_root = (
            await self._workspaces.resolve(thread_id=normalized, record=record)
        ).root

        actor_id = LOCAL_ACTOR_ID
        meta_error = ""
        try:
            meta_deleted = await self._thread_store.delete(normalized)
        except Exception as exc:
            # WHY 不向上抛：元数据只用于清单展示，让它把一次删除操作变成 500 会
            # 误导调用方以为整个操作失败；但失败原因必须带回给调用方，
            # 否则「删不掉」就成了一个没有原因的结果。
            logger.exception("删除会话元数据失败：thread=%s", normalized)
            meta_deleted = False
            meta_error = str(exc) or type(exc).__name__

        checkpoint_removed = await self._delete_checkpoints(normalized)
        # WHY 一并删附件：它们是工作区里的真实文件，而会话已不再被任何界面引用——
        # 留着不占功能，只占磁盘，且没有任何入口能看到或清掉它们。
        attachments_removed = await self._delete_attachments(normalized, workspace_root)

        if meta_deleted and checkpoint_removed:
            outcome = DeleteOutcome.DELETED
        elif meta_deleted:
            outcome = DeleteOutcome.PARTIAL
        elif meta_error:
            outcome = DeleteOutcome.FAILED
        else:
            # WHY 判定以元数据为准：会话清单读的就是 thread_meta，它的增删才决定
            # 会话在界面上「是否存在」。而 checkpointer 的删除接口对不存在的会话
            # 是静默成功的 no-op，无法用来判断会话是否真的存在过。
            outcome = DeleteOutcome.NOT_FOUND

        # WHY 只在真正有残留/失败时给 detail：会话本就不存在时检查点删除是
        # 一次成功的 no-op，此时返回失败文案会让调用方误以为需要重试。
        if outcome is DeleteOutcome.PARTIAL:
            detail = "检查点清理失败，历史记录可能残留"
        elif outcome is DeleteOutcome.FAILED:
            detail = meta_error
        else:
            detail = ""

        await self._audit(
            event_type="thread_delete",
            actor_id=actor_id,
            target_id=normalized,
            action="delete",
            outcome=outcome.value,
            details={
                "checkpoint_removed": checkpoint_removed,
                "attachments_removed": attachments_removed,
                "detail": detail,
            },
        )

        logger.info(
            "删除会话完成：thread=%s outcome=%s checkpoint=%s actor=%s",
            normalized,
            outcome.value,
            checkpoint_removed,
            actor_id,
        )
        return DeleteResult(
            thread_id=normalized,
            outcome=outcome,
            checkpoint_removed=checkpoint_removed,
            detail=detail,
        )

    async def _delete_attachments(self, thread_id: str, workspace_root: Path) -> int:
        """删除该会话在指定工作区里的附件文件。

        WHY 由调用方传入工作区根而不是在这里解析：本方法在元数据**已删除之后**才被
        调用，此刻会话行已经没有了——再解析只能回落默认工作区，而附件在别处。

        WHY 失败不上抛：会话删除的主结果由元数据决定，附件残留只是空间问题；
        把它升级成一次失败的删除，会让用户为了几个文件重试一次已经成功的操作。
        删除失败会写日志，并随审计的 ``details`` 一并留痕。

        Returns:
            实际删除的附件数；出错时为 0。
        """
        try:
            return await asyncio.to_thread(
                delete_thread_attachments, workspace_root, thread_id
            )
        except Exception:
            logger.exception("删除会话附件失败：thread=%s", thread_id)
            return 0

    async def _delete_checkpoints(self, thread_id: str) -> bool:
        """删除该会话的全部检查点。

        Args:
            thread_id: 会话 ID。

        Returns:
            ``True`` 表示删除动作成功执行（本就没有记录的会话也算成功）；
            ``False`` 表示当前 checkpointer 不支持删除或执行时抛错。
        """
        deleter = getattr(self._checkpointer, "adelete_thread", None)
        if deleter is None:
            logger.warning("当前 checkpointer 不支持删除会话：thread=%s", thread_id)
            return False

        try:
            await deleter(thread_id)
        except Exception:
            logger.exception("删除会话检查点失败：thread=%s", thread_id)
            return False

        return True

    async def rename_thread(
        self,
        thread_id: str,
        title: str,
    ) -> ThreadSummary:
        """重命名会话。

        WHY 由所有者自行改名：自动标题取自首条输入，常常无法概括整段对话
        （「帮我看看这个」），改名是用户让清单可读的唯一手段。

        Args:
            thread_id: 会话 ID。
            title: 新标题；内部空白会被折叠为单个空格。

        Returns:
            更新后的会话摘要。

        Raises:
            ValueError: 标题非字符串、为空或超出 ``thread_rename_max_chars``。
            NotFoundError: 会话不存在。
            RuntimeError: 写入失败。
        """
        normalized = normalize_thread_id(thread_id)
        normalized_title = self._normalize_rename_title(title)

        record = await self._require_record(normalized)

        try:
            updated = await self._thread_store.rename(normalized, normalized_title)
        except ValueError:
            # 存储层再次校验（硬上限），参数错误仍应由路由映射为 400
            raise
        except Exception as exc:
            logger.exception("重命名会话失败：thread=%s", normalized)
            raise RuntimeError(f"重命名会话失败：thread={normalized}") from exc

        if updated is None:
            # 校验通过后被并发删除：按「不存在」处理，而不是返回一份空摘要
            raise NotFoundError("会话", normalized)

        actor_id = LOCAL_ACTOR_ID
        await self._audit(
            event_type="thread_rename",
            actor_id=actor_id,
            target_id=normalized,
            action="rename",
            outcome="success",
            details={"title": normalized_title},
        )
        logger.info("会话已重命名：thread=%s actor=%s", normalized, actor_id)
        return ThreadSummary(**updated)

    async def set_archived(
        self,
        thread_id: str,
        archived: bool,
    ) -> ThreadSummary:
        """归档或恢复会话（软删除）。

        WHY 归档而不是删除：删除会连带清掉检查点且不可恢复，而用户多数时候只是
        想让清单干净，过一阵还想翻回来。归档只改变清单可见性——历史仍可读、
        用量仍保留、正在运行的任务不受影响；真正销毁数据仍然是 ``delete_thread``。

        Args:
            thread_id: 会话 ID。
            archived: ``True`` 归档，``False`` 恢复。

        Returns:
            更新后的会话摘要。

        Raises:
            ValueError: ``thread_id`` 非法或 ``archived`` 不是布尔值。
            NotFoundError: 会话不存在。
            RuntimeError: 写入失败。
        """
        normalized = normalize_thread_id(thread_id)
        if not isinstance(archived, bool):
            raise ValueError(f"archived 必须是布尔值，实际：{type(archived).__name__}")

        record = await self._require_record(normalized)

        try:
            updated = await self._thread_store.set_archived(normalized, archived)
        except ValueError:
            raise
        except Exception as exc:
            logger.exception("设置会话归档状态失败：thread=%s", normalized)
            raise RuntimeError(f"设置会话归档状态失败：thread={normalized}") from exc

        if updated is None:
            raise NotFoundError("会话", normalized)

        actor_id = LOCAL_ACTOR_ID
        await self._audit(
            event_type="thread_archive",
            actor_id=actor_id,
            target_id=normalized,
            action="archive" if archived else "unarchive",
            outcome="success",
            details={"archived": archived},
        )
        logger.info("会话归档状态已更新：thread=%s archived=%s actor=%s", normalized, archived, actor_id)
        return ThreadSummary(**updated)

    # ------------------------------------------------------------------ 内部

    def _normalize_rename_title(self, title: Any) -> str:
        """校验并归一重命名标题。

        WHY 在服务层就拒绝超长而不是留给存储层截断：用户输入的标题被静默改写，
        是「界面不听话」的典型来源；报错才能让人知道要缩短到什么程度。

        Args:
            title: 原始标题。

        Returns:
            折叠空白后的标题。

        Raises:
            ValueError: 非字符串、为空或超出配置的字符上限。
        """
        if not isinstance(title, str):
            raise ValueError(f"title 必须是字符串，实际：{type(title).__name__}")
        collapsed = collapse_whitespace(title)
        if not collapsed:
            raise ValueError("title 不能为空")
        limit = self._config.thread_rename_max_chars
        if len(collapsed) > limit:
            raise ValueError(f"title 过长（{len(collapsed)} > {limit}）")
        return collapsed

    @staticmethod
    def _message_to_dto(
        message: Any, attachment_index: dict[str, AttachmentRecord] | None = None
    ) -> HistoryMessage:
        """把 LangChain 消息转成对外 DTO。

        WHY 必须显式处理列表型 content：带附件的用户消息是多模态内容块列表，而旧
        实现用 ``str(content)`` 转换——那会把 base64 图片内容整个塞进历史响应
        （一条消息几 MB），而且它在界面上「看起来有内容」，不会报错。
        """
        tool_calls = getattr(message, "tool_calls", None) or []
        content = getattr(message, "content", "")
        return HistoryMessage(
            role=getattr(message, "type", "") or "",
            content=_content_text(content),
            name=getattr(message, "name", "") or "",
            tool_calls=[
                {
                    # WHY 保留 call id：工具结果消息靠它指回是哪一次调用。丢了它，
                    # 导出的文件再导回去时工具消息会变成孤儿——要么被丢弃，要么
                    # 挂到错误的调用上，而两者都不会报错。
                    "id": call.get("id", ""),
                    "name": call.get("name", ""),
                    "args": call.get("args", {}),
                }
                for call in tool_calls
            ],
            tool_call_id=getattr(message, "tool_call_id", "") or "",
            attachments=_attachments_of(content, attachment_index or {}),
        )


_TEXT_BLOCK_TYPES = frozenset({"text", "input_text"})
"""内容块里属于「文本」的类型名。

WHY 同时认 ``input_text``：它是 LangChain 标准内容块的另一种写法，只认 ``text``
会让某些 provider 回写的消息在历史里变成空正文。
"""

_IMAGE_BLOCK_TYPES = frozenset({"image_url", "input_image"})
"""内容块里属于「图片」的类型名；与 ``_TEXT_BLOCK_TYPES`` 对称。"""

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[^;,]+)(?P<params>[^,]*);base64,(?P<payload>.+)$",
    re.IGNORECASE | re.DOTALL,
)
"""匹配图片块的 data URL，并拆出 MIME、参数与 base64 载荷。

WHY 必须拆出载荷而不是只做前缀判断：附件是按内容摘要（sha256）匹配回来的，只有拿到
base64 载荷并解码才能算出摘要、与附件目录对上号；只判断「它是不是 data URL」拿不到
这个对应关系，历史里的图就会全部显示不出来。
"""


def _content_text(content: Any) -> str:
    """取消息正文的文本部分。

    WHY 只取文本块而不是把整个列表转成字符串：列表里可能含着 base64 图片内容，
    转成字符串会把几 MB 的编码塞进历史响应与导出文件——而且它看起来「有内容」，
    不会报错，只会在某天变成一次超时或一次 OOM。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in _TEXT_BLOCK_TYPES
        )
    return str(content or "")


def _image_url_of(part: dict[str, Any]) -> str:
    """从图片内容块里取出 URL 字符串；取不到时返回空串。"""
    raw = part.get("image_url")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        url = raw.get("url")
        return url if isinstance(url, str) else ""
    return ""


def _attachments_of(
    content: Any, index: dict[str, AttachmentRecord]
) -> list[AttachmentInfo]:
    """从多模态内容块里回填附件信息。

    WHY 用内容摘要（sha256）匹配而不是在块里塞一个 ID 字段：内容块会被原样发给
    模型，多出来的字段要么被拒绝、要么被当作未知参数透传；而摘要由内容算出，
    两侧都不需要额外约定。匹配不到的图片直接略过——那通常意味着附件已被删除。
    """
    if not isinstance(content, list) or not index:
        return []

    found: list[AttachmentInfo] = []
    seen: set[str] = set()
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in _IMAGE_BLOCK_TYPES:
            continue
        match = _DATA_URL_RE.match(_image_url_of(part))
        if match is None:
            continue
        try:
            payload = base64.b64decode(match.group("payload"), validate=True)
        except (binascii.Error, ValueError):
            # 不是合法的 base64：可能是 provider 回写的其它形态，跳过而不是让整次
            # 历史读取失败——一张图显示不出来不该让整个会话打不开。
            logger.debug("历史消息里的图片块不是合法 data URL，已跳过")
            continue
        record = index.get(hashlib.sha256(payload).hexdigest())
        if record is None or record.id in seen:
            continue
        seen.add(record.id)
        found.append(attachment_info(record))
    return found
