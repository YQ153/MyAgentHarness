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

from application.audit_context import audit_client_info
from application.dto import (
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
from runtime.thread_store import ThreadMetaStore, normalize_thread_id

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
        """记录一条审计事件，并自动补上当前请求的 IP / User-Agent。

        WHY IP/UA 由本方法统一补齐：调用点只关心「记了什么」，来源信息由
        接口层中间件写入的 ``contextvars`` 提供，任一调用点都不会漏；审计
        失败同样不上抛——它是旁路职责，不应把一次成功的删除变成 500。
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
    ) -> ThreadListResult:
        """列出会话清单，最近活动的在前。

        WHY 只读元数据表而不遍历每个会话的图状态：后者需要对每个 thread 调用
        一次 ``aget_state``，成本随会话数线性增长；而列表页只需要标题与时间。

        Args:
            principal: 当前主体；``None`` 仅在认证关闭时使用。
            limit: 返回条数，1..200。
            offset: 跳过的条数，用于分页。

        Returns:
            会话清单与总数。

        Raises:
            ValueError: 分页参数非法（调用方应映射为 400）。
            RuntimeError: 查询失败（调用方应映射为 500）。
        """
        owner_id = self._effective_owner_id(principal)
        try:
            items = await self._thread_store.list_threads(
                owner_id=owner_id, limit=limit, offset=offset
            )
            total = await self._thread_store.count(owner_id=owner_id)
        except ValueError:
            # WHY 让参数错误原样透出：路由层需要把它映射为 400 而不是 500
            raise
        except Exception as exc:
            logger.exception("查询会话列表失败：limit=%s offset=%s", limit, offset)
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
    ) -> list[HistoryMessage]:
        """读取会话历史，供前端刷新页面后恢复上下文。

        WHY 用 ``aget_state`` 而不用同步的 ``get_state``：异步检查点保存器
        只实现了异步接口，调用同步方法会抛 ``NotImplementedError``。

        WHY 只在真正查不到状态时返回空列表，而不再吞掉异常：此前所有异常都降级
        成 ``[]``，会把「数据库不可用」伪装成「这个会话没有历史」，前端与调用方
        都无法区分，故障也就不可观测。``aget_state`` 对不存在的会话返回的是空
        状态快照而不是异常，因此空列表仍然精确对应「无历史」。

        Args:
            thread_id: 会话 ID。
            principal: 当前主体；``None`` 仅在认证关闭时使用。

        Returns:
            历史消息列表；会话不存在时为空列表。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话元数据不存在。
            OwnershipError: 无权访问该会话。
            RuntimeError: 读取失败。
        """
        normalized = normalize_thread_id(thread_id)
        record = await self._thread_store.get(normalized)
        self._ensure_ownership(record, normalized, principal)

        graph = self._graph_factory.get()

        try:
            state = await graph.aget_state(
                build_runnable_config(self._config, normalized)
            )
        except Exception as exc:
            logger.exception("读取会话历史失败：thread=%s", normalized)
            raise RuntimeError(f"读取会话历史失败：thread={normalized}") from exc

        if state is None:
            return []

        messages = getattr(state, "values", {}).get("messages") or []
        return [self._message_to_dto(message) for message in messages]

    # ------------------------------------------------------------------ 写入

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

    # ------------------------------------------------------------------ 内部

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
