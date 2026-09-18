"""会话服务：会话标识、清单、历史与删除。

职责边界：只编排「会话」这一聚合的读写，不涉及运行期事件流
（见 ``application.run_service.RunService``）。两者此前合并在一个类里，
导致任何一处改动都要牵动另一处的依赖，也让单元测试必须同时构造图、
检查点与元数据存储。
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from application.audit_context import audit_client_info, audit_trace_id
from application.dto import (
    BranchListResult,
    BranchSummary,
    DeleteOutcome,
    DeleteResult,
    HistoryMessage,
    ThreadListResult,
    ThreadSummary,
)
from application.errors import NotFoundError, OwnershipError
from application.ownership import effective_owner_id, ensure_thread_access
from application.principal import Principal
from application.runnable import build_runnable_config
from runtime.audit_store import AuditStore
from runtime.thread_store import (
    ThreadMetaStore,
    normalize_search_query,
)
from text_utils import collapse_whitespace
from thread_utils import normalize_thread_id

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from agent.graph import AgentFactory
    from config import AppConfig

logger = logging.getLogger(__name__)


class ThreadService:
    """管理会话的元数据生命周期。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        checkpointer: BaseCheckpointSaver,
        thread_store: ThreadMetaStore,
        graph_factory: AgentFactory,
        audit_store: AuditStore | None = None,
    ) -> None:
        """构造会话服务。

        Args:
            config: 应用配置。
            checkpointer: 检查点保存器，用于清理会话状态。
            thread_store: 会话元数据存储。
            graph_factory: 图工厂，用于读取会话历史。
            audit_store: 审计日志存储，可选；认证关闭时可为 ``None``。

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

        self._config = config
        self._checkpointer = checkpointer
        self._thread_store = thread_store
        self._graph_factory = graph_factory
        self._audit_store = audit_store

        logger.info("ThreadService 就绪：workspace=%s auth_mode=%s", config.workspace, config.auth_mode)

    def _effective_owner_id(self, principal: Principal | None) -> str | None:
        """根据认证模式返回查询时使用的 owner_id。

        判定规则见 ``application.ownership.effective_owner_id``——此处只做
        转发，保证本服务与运行服务、用量服务的口径完全一致。
        """
        return effective_owner_id(self._config, principal)

    def _ensure_ownership(
        self,
        record: dict[str, Any] | None,
        thread_id: str,
        principal: Principal | None,
        require_admin: bool = False,
    ) -> None:
        """校验主体是否拥有该会话的访问权。

        Raises:
            NotFoundError: 会话不存在。
            OwnershipError: 会话存在但当前主体无权访问。
        """
        ensure_thread_access(
            record,
            thread_id,
            self._config,
            principal,
            require_admin=require_admin,
        )

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
        而端点当前没有鉴权，客户端可自选 ID 意味着任何人都能覆盖他人会话。
        ``uuid4`` 不可预测，这个属性必须保留。

        Returns:
            新会话的 ID；此刻数据库中尚未产生任何记录。
        """
        return uuid.uuid4().hex

    async def list_threads(
        self,
        principal: Principal | None = None,
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
            principal: 当前主体；``None`` 仅在认证关闭时使用。
            limit: 返回条数，1..200。
            offset: 跳过的条数，用于分页。
            query: 标题关键字；``None`` 表示不过滤。搜索只覆盖标题——正文检索
                需要反序列化检查点，属另一个量级的工作（见项目文档已知限制）。
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
        owner_id = self._effective_owner_id(principal)
        try:
            # WHY 两个查询传完全相同的过滤条件：总数与实际返回条数必须对得上，
            # 否则前端会显示「还有下一页」但翻过去是空的。
            items = await self._thread_store.list_threads(
                owner_id=owner_id,
                limit=limit,
                offset=offset,
                query=normalized_query,
                tag=tag,
                include_archived=include_archived,
            )
            total = await self._thread_store.count(
                owner_id=owner_id,
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
        principal: Principal | None = None,
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
            principal: 当前主体；``None`` 仅在认证关闭时使用。
            branch_id: 要读取的分支；``None`` 表示当前分支。

        Returns:
            该分支的历史消息列表；会话不存在时为空列表。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话元数据或指定分支不存在。
            OwnershipError: 无权访问该会话。
            RuntimeError: 读取失败。
        """
        normalized = normalize_thread_id(thread_id)
        record = await self._thread_store.get(normalized)
        self._ensure_ownership(record, normalized, principal)

        graph = self._graph_factory.get()
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
        return [self._message_to_dto(message) for message in messages]

    # ------------------------------------------------------------------ 分支

    async def list_branches(
        self,
        thread_id: str,
        principal: Principal | None = None,
    ) -> BranchListResult:
        """列出会话的全部分支，并标出当前分支。

        WHY 把「当前分支」一并返回：界面要据此高亮，而它本来就是会话行上的一列——
        让调用方逐个分支去问一次既慢又容易与切换结果不一致。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话元数据不存在。
            OwnershipError: 无权访问该会话。
        """
        normalized = normalize_thread_id(thread_id)
        record = await self._thread_store.get(normalized)
        self._ensure_ownership(record, normalized, principal)

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
        principal: Principal | None = None,
    ) -> BranchListResult:
        """把某条分支设为当前分支。

        WHY 切换前必须冻结原来的当前分支：它的头随每次运行前移，一旦不再是当前分支就
        没有任何地方能说明它停在哪——不冻结的话，它下次被切回来会接到会话最新状态上，
        也就是接错分支。这正是「分叉之后必须自己记分支头」的直接后果。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话或指定分支不存在。
            OwnershipError: 无权访问该会话。
        """
        normalized = normalize_thread_id(thread_id)
        record = await self._thread_store.get(normalized)
        self._ensure_ownership(record, normalized, principal)

        if branch_id != "":
            target = await self._thread_store.get_branch(normalized, branch_id)
            if target is None:
                raise NotFoundError("分支", branch_id)

        current = await self._thread_store.current_branch(normalized)
        if current != branch_id:
            # WHY 不无条件冻结原分支：此刻会话头属于**最新**那条分支，未必是正要离开
            # 的这一条。只在它「从未被离开过」时补记一次——那种情况下会话头正是它自己
            # 的头（详见 ``RunService._freeze_unrecorded`` 的归纳）。
            existing = await self._thread_store.get_branch(normalized, current)
            if not (existing or {}).get("head_checkpoint"):
                head = await self._live_head(normalized)
                if head:
                    await self._thread_store.set_branch_head(normalized, current, head)
            await self._thread_store.set_current_branch(normalized, branch_id)
            await self._audit(
                event_type="thread_branch",
                # WHY 与 RunService 同一口径：审计主体是「谁做的」，不是查询过滤值——
                # effective_owner_id 在认证关闭时返回 None、未认证时返回占位常量，
                # 拿它当主体会让审计里出现一个不是任何人的标识。
                actor_id=principal.user_id if principal else "anonymous",
                target_id=normalized,
                action="activate",
                outcome="success",
                details={"branch_id": branch_id, "frozen_branch_id": current},
            )
            logger.info(
                "会话切换分支：thread=%s from=%s to=%s", normalized, current, branch_id
            )

        return await self.list_branches(normalized, principal)

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
        graph = self._graph_factory.get()
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
        principal: Principal | None = None,
    ) -> ThreadSummary:
        """整体替换会话标签。

        WHY 用 ``thread:update`` 而不是新开权限：打标签与改名、归档同属「所有者
        整理自己的清单」，风险量级一致；新开一个权限只会让只想授予整理能力的角色
        拿不到标签功能。

        Raises:
            ValueError: ``thread_id`` 非法或标签不合法。
            NotFoundError: 会话不存在。
            OwnershipError: 无权访问该会话。
        """
        normalized = normalize_thread_id(thread_id)
        record = await self._thread_store.get(normalized)
        self._ensure_ownership(record, normalized, principal)

        updated = await self._thread_store.set_tags(normalized, tags)
        if updated is None:
            raise NotFoundError("会话", normalized)

        await self._audit(
            event_type="thread_tags",
            actor_id=principal.user_id if principal else "anonymous",
            target_id=normalized,
            action="update",
            outcome="success",
            details={"tags": updated.get("tags") or []},
        )
        logger.info("会话标签已更新：thread=%s tags=%s", normalized, updated.get("tags"))
        return ThreadSummary(**updated)

    async def delete_thread(
        self,
        thread_id: str,
        principal: Principal | None = None,
    ) -> DeleteResult:
        """删除会话：检查点与元数据一并清理。

        WHY 直接调用 checkpointer 的删除接口：图的状态读取无法区分「空会话」
        与「不存在」，而 ``update_state`` 只会追加写入，始终删不掉历史记录。

        WHY 返回结构化结果而非布尔值：``False`` 会同时表示「本来就不存在」
        「元数据删除失败」「检查点清理失败」三种含义，调用方无法判断该提示用户
        还是该重试。

        Args:
            thread_id: 会话 ID。
            principal: 当前主体；``None`` 仅在认证关闭时使用。

        Returns:
            删除结果，含结果分类、检查点是否清理与失败摘要。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话不存在。
            OwnershipError: 无权删除该会话。
        """
        normalized = normalize_thread_id(thread_id)

        # WHY 先查再删：避免在无权访问时通过「删除不存在」的响应泄露会话存在性。
        record = await self._thread_store.get(normalized)
        self._ensure_ownership(record, normalized, principal)

        actor_id = principal.user_id if principal else "anonymous"
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
            details={"checkpoint_removed": checkpoint_removed, "detail": detail},
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
        principal: Principal | None = None,
    ) -> ThreadSummary:
        """重命名会话。

        WHY 由所有者自行改名：自动标题取自首条输入，常常无法概括整段对话
        （「帮我看看这个」），改名是用户让清单可读的唯一手段。

        Args:
            thread_id: 会话 ID。
            title: 新标题；内部空白会被折叠为单个空格。
            principal: 当前主体；``None`` 仅在认证关闭时使用。

        Returns:
            更新后的会话摘要。

        Raises:
            ValueError: 标题非字符串、为空或超出 ``thread_rename_max_chars``。
            NotFoundError: 会话不存在。
            OwnershipError: 无权修改该会话。
            RuntimeError: 写入失败。
        """
        normalized = normalize_thread_id(thread_id)
        normalized_title = self._normalize_rename_title(title)

        # WHY 先查再改：与删除同源——无权访问时不能通过响应差异泄露会话是否存在。
        record = await self._thread_store.get(normalized)
        self._ensure_ownership(record, normalized, principal)

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

        actor_id = principal.user_id if principal else "anonymous"
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
        principal: Principal | None = None,
    ) -> ThreadSummary:
        """归档或恢复会话（软删除）。

        WHY 归档而不是删除：删除会连带清掉检查点且不可恢复，而用户多数时候只是
        想让清单干净，过一阵还想翻回来。归档只改变清单可见性——历史仍可读、
        用量仍保留、正在运行的任务不受影响；真正销毁数据仍然是 ``delete_thread``。

        Args:
            thread_id: 会话 ID。
            archived: ``True`` 归档，``False`` 恢复。
            principal: 当前主体；``None`` 仅在认证关闭时使用。

        Returns:
            更新后的会话摘要。

        Raises:
            ValueError: ``thread_id`` 非法或 ``archived`` 不是布尔值。
            NotFoundError: 会话不存在。
            OwnershipError: 无权修改该会话。
            RuntimeError: 写入失败。
        """
        normalized = normalize_thread_id(thread_id)
        if not isinstance(archived, bool):
            raise ValueError(f"archived 必须是布尔值，实际：{type(archived).__name__}")

        record = await self._thread_store.get(normalized)
        self._ensure_ownership(record, normalized, principal)

        try:
            updated = await self._thread_store.set_archived(normalized, archived)
        except ValueError:
            raise
        except Exception as exc:
            logger.exception("设置会话归档状态失败：thread=%s", normalized)
            raise RuntimeError(f"设置会话归档状态失败：thread={normalized}") from exc

        if updated is None:
            raise NotFoundError("会话", normalized)

        actor_id = principal.user_id if principal else "anonymous"
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
    def _message_to_dto(message: Any) -> HistoryMessage:
        """把 LangChain 消息转成对外 DTO。"""
        tool_calls = getattr(message, "tool_calls", None) or []
        return HistoryMessage(
            role=getattr(message, "type", "") or "",
            content=(
                message.content
                if isinstance(message.content, str)
                else str(message.content)
            ),
            name=getattr(message, "name", "") or "",
            tool_calls=[
                {
                    "name": call.get("name", ""),
                    "args": call.get("args", {}),
                }
                for call in tool_calls
            ],
        )
