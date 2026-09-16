"""运行服务：发起对话、人工审批后恢复执行。

职责边界：只负责「把一次运行推进到底并产出事件」，不负责会话清单与历史
（见 ``application.thread_service.ThreadService``）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from application.errors import NotFoundError, OwnershipError, PermissionDeniedError, ThreadBusyError
from application.event_translator import LangGraphEventTranslator
from application.events import AgentEvent, AgentEventType
from application.interrupt_codec import build_resume_command
from application.principal import Principal
from application.runnable import build_runnable_config
from runtime.audit_store import AuditStore
from runtime.thread_store import ThreadMetaStore, normalize_thread_id
from text_utils import build_title

if TYPE_CHECKING:
    from agent.graph import AgentFactory
    from config import AppConfig

logger = logging.getLogger(__name__)

_STREAM_MODES = ["messages", "updates"]


@dataclass(frozen=True, eq=False)
class RunHandle:
    """一次运行中会话的运行句柄。

    WHY 独立成类而不是继续用裸集合：停止（``stop``）、运行超时（后续迭代）
    与运行指标（``/metrics``）都需要「thread_id → 取消信号 + 开始时间」这
    同一份登记，各自另写一套必然出现口径不一致（例如超时任务看到的运行
    集合与 stop 看到的不一致）。

    eq=False：句柄的身份就是对象本身，按字段比较两个句柄（含 ``Event``）
    没有意义，反而容易在集合操作中被误判相等。
    """

    thread_id: str
    started_at: float
    """``time.monotonic()`` 口径的开始时间，用于超时判断与指标。"""
    cancel_event: asyncio.Event
    """停止信号；置位后运行在下一个分片边界被中止。"""

    @property
    def stop_requested(self) -> bool:
        """是否已收到停止请求。"""
        return self.cancel_event.is_set()

    @property
    def elapsed_seconds(self) -> float:
        """已运行时长（秒）。"""
        return time.monotonic() - self.started_at

    def request_stop(self) -> None:
        """请求停止本次运行；重复调用幂等。"""
        self.cancel_event.set()


class _RunStoppedError(Exception):
    """内部信号：运行因用户停止请求而中止。

    WHY 私有：这是 ``_stream_graph`` 与 ``_iterate`` 之间的控制流协议，
    不属于服务的对外契约；对外的「已停止」表达是 DONE 事件的 reason 字段。
    """

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"会话 {thread_id} 的运行已被停止")
        self.thread_id = thread_id


class RunService:
    """推进一次对话运行，并把 LangGraph 的原始流翻译成统一事件。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        thread_store: ThreadMetaStore,
        graph_factory: AgentFactory,
        audit_store: AuditStore | None = None,
    ) -> None:
        """构造运行服务。

        Args:
            config: 应用配置。
            thread_store: 会话元数据存储，用于登记轮次与刷新活动时间。
            graph_factory: 图工厂，提供已装配的 LangGraph 图。
            audit_store: 审计日志存储，可选。

        Raises:
            ValueError: 任一必需依赖为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if thread_store is None:
            raise ValueError("thread_store 不能为 None")
        if graph_factory is None:
            raise ValueError("graph_factory 不能为 None")

        self._config = config
        self._thread_store = thread_store
        self._graph_factory = graph_factory
        self._audit_store = audit_store

        # WHY 用 threading.Lock 保护「运行中」登记表：加解锁之间不 await，
        # 临界区极短；更重要的是释放动作必须能在 finally 里同步完成——
        # 若用 asyncio.Lock，客户端断开连接触发 GeneratorExit 时在 finally
        # 中 await 会破坏生成器的关闭流程。
        self._running: dict[str, RunHandle] = {}
        self._running_guard = threading.Lock()

        logger.info(
            "RunService 就绪：mode=%s recursion_limit=%s",
            config.execution_mode.value,
            config.recursion_limit,
        )

    def _owner_id(self, principal: Principal | None) -> str:
        """返回写入 thread_meta 的 owner_id。"""
        if self._config.auth_mode == "disabled" or principal is None:
            return ""
        return principal.user_id

    def _ensure_permission(
        self,
        principal: Principal | None,
        permission: str,
    ) -> None:
        """校验主体是否拥有某权限。"""
        if self._config.auth_mode == "disabled":
            return
        if principal is None or not principal.has_permission(permission):
            raise PermissionDeniedError(permission)

    async def _ensure_ownership(
        self,
        thread_id: str,
        principal: Principal | None,
        *,
        allow_claim: bool = False,
    ) -> dict[str, Any]:
        """校验主体是否拥有该会话，并返回元数据记录。

        Args:
            allow_claim: 允许在未登记时「认领」该会话（用于 ``stream`` 首条消息场景）。

        Raises:
            NotFoundError: 会话不存在且 ``allow_claim=False``。
            OwnershipError: 无权访问。
        """
        record = await self._thread_store.get(thread_id)
        if record is None:
            if allow_claim:
                return {}
            raise NotFoundError("会话", thread_id)
        if self._config.auth_mode == "disabled":
            return record
        if principal is None:
            raise OwnershipError("会话", thread_id)
        if principal.is_admin():
            return record
        owner_id = record.get("owner_id") or ""
        if owner_id and owner_id != principal.user_id:
            raise OwnershipError("会话", thread_id)
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
        if self._audit_store is None:
            return
        await self._audit_store.log(
            event_type=event_type,
            actor_id=actor_id,
            target_id=target_id,
            action=action,
            outcome=outcome,
            details=details,
        )

    # ------------------------------------------------------------------ 运行

    async def stream(
        self,
        thread_id: str,
        user_input: str,
        *,
        principal: Principal | None = None,
        model_name: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """发起一轮对话。

        WHY 本方法是 ``async def`` 且**不含** ``yield``：异步生成器要等到首次
        ``__anext__()`` 才执行函数体，参数校验与模型初始化若写在里面，就要等到
        响应已经开始之后才抛错，客户端只能看到「连接被中断」。写成普通协程可以
        让这些前置失败在 ``await service.stream(...)`` 时就抛出，调用方得以返回
        正常的 HTTP 状态码。

        Args:
            thread_id: 会话 ID。
            user_input: 用户本轮输入，不能为空。
            principal: 当前主体；``None`` 仅在认证关闭时使用。
            model_name: 模型别名；``None`` 表示使用默认模型。

        Returns:
            产出统一事件的异步迭代器。

        Raises:
            ValueError: ``thread_id`` 或 ``user_input`` 非法。
            KeyError: 模型别名未注册。
            PermissionDeniedError: 缺少 thread:create 权限。
            NotFoundError: 会话不存在。
            OwnershipError: 无权访问该会话。
            RuntimeError: 模型初始化或装配失败。
        """
        self._ensure_permission(principal, "thread:create")

        normalized = normalize_thread_id(thread_id)
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("user_input 必须是非空字符串")
        text = user_input.strip()

        # WHY 先鉴权再初始化模型：权限不足应快速失败，避免浪费模型调用。
        # 首条消息可能还未登记元数据，允许当前主体认领该会话。
        await self._ensure_ownership(normalized, principal, allow_claim=True)

        # WHY 在进入图之前取图：这一步会解析模型别名并真正初始化模型，
        # 把配置与密钥错误暴露在事件流开始之前。
        graph = self._graph_factory.get(model_name)

        actor_id = principal.user_id if principal else "anonymous"
        logger.info("会话 %s 发起运行（%d 字符）actor=%s", normalized, len(text), actor_id)

        # WHY 在进入图之前登记：这一刻才是会话真正诞生的时刻。放在轮次结束后
        # 登记，会让「模型初始化失败」这类早退场景下的会话凭空消失，而用户
        # 明明已经表达过意图。标题也取自这次输入——唯一「用户明确表达意图」
        # 的文本，不需要额外调用模型。
        recorded = await self._record_turn(normalized, title_hint=text, turn_delta=1, principal=principal)

        # WHY 登记后再校验一次所有权：并发首条消息场景下，UPSERT 会以首个写入者
        # 的 owner_id 为准；登记后回读可发现该会话是否已被他人抢先认领，
        # 避免后续运行写入错误的 owner 上下文。
        if recorded is not None and self._config.auth_mode != "disabled":
            recorded_owner = recorded.get("owner_id") or ""
            expected_owner = self._owner_id(principal)
            is_admin = principal is not None and principal.is_admin()
            if recorded_owner and recorded_owner != expected_owner and not is_admin:
                logger.warning(
                    "会话认领冲突：thread=%s expected_owner=%s actual_owner=%s",
                    normalized, expected_owner, recorded_owner,
                )
                raise OwnershipError("会话", normalized)

        await self._audit(
            event_type="thread_run",
            actor_id=actor_id,
            target_id=normalized,
            action="stream",
            outcome="success",
        )

        # WHY 在返回生成器之前占用运行槽位：占用动作若留在生成器体内，就要等到
        # 首个事件被拉取时才执行，此时响应已经开始，ThreadBusyError 只能表现为
        # 连接中断。占用成功后紧接返回生成器，中间不再有任何可能失败的语句，
        # 因此不会出现「占了槽位却没人为它收尾」。
        handle = self._acquire_run_slot(normalized)

        payload: dict[str, Any] = {"messages": [{"role": "user", "content": text}]}
        return self._consume(graph, payload, handle)

    async def resume(
        self,
        thread_id: str,
        decision_payload: Any,
        *,
        principal: Principal | None = None,
        model_name: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """人工审批后恢复被中断的执行。

        WHY 与 ``stream`` 分开：恢复的输入是 ``Command`` 而非用户消息，
        混在一个方法里会让调用方难以判断当前处于哪种状态。

        Args:
            thread_id: 会话 ID。
            decision_payload: 审批结果，形如 ``{"decisions": [{"type": "approve"}]}``。
            principal: 当前主体；``None`` 仅在认证关闭时使用。
            model_name: 模型别名；``None`` 表示使用默认模型。

        Returns:
            产出统一事件的异步迭代器。

        Raises:
            ValueError: ``thread_id`` 非法，或审批载荷格式非法。
            KeyError: 模型别名未注册。
            NotFoundError: 会话不存在。
            OwnershipError: 无权访问该会话。
            RuntimeError: 模型初始化或装配失败。
        """
        normalized = normalize_thread_id(thread_id)
        # WHY 在这里就完成审批载荷校验：非法载荷必须在事件流开始之前失败，
        # 否则只能表现为连接中断，前端拿不到任何可读的失败原因。
        command = build_resume_command(decision_payload)
        graph = self._graph_factory.get(model_name)

        await self._ensure_ownership(normalized, principal)

        actor_id = principal.user_id if principal else "anonymous"
        logger.info("会话 %s 恢复执行 actor=%s", normalized, actor_id)

        # WHY turn_delta=0：恢复是同一轮运行的延续，重复计数会让「对话轮数」
        # 与实际用户输入次数不符；但仍然要刷新活动时间。
        await self._record_turn(normalized, title_hint=None, turn_delta=0)

        decisions = decision_payload.get("decisions") or []
        await self._audit(
            event_type="hitl_decision",
            actor_id=actor_id,
            target_id=normalized,
            action="resume",
            outcome="success",
            details={"decisions": [d.get("type") for d in decisions]},
        )

        handle = self._acquire_run_slot(normalized)
        return self._consume(graph, command, handle)

    async def stop(
        self,
        thread_id: str,
        *,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        """请求停止指定会话的当前运行。

        语义：只「触发」取消而不等待运行真正结束——已产出但尚未送达的事件
        会继续推送，运行最终以 DONE（payload 含 ``reason: "stopped"``）收尾。

        幂等：会话未在运行时返回 ``stopped=False``；对已请求过停止的会话
        重复调用返回 ``stopped=True``。二者都不是错误——「连点停止按钮」与
        「运行恰好在请求前一刻自然结束」不应让用户看到报错。

        Args:
            thread_id: 会话 ID。
            principal: 当前主体；``None`` 仅在认证关闭时使用。

        Returns:
            ``{"thread_id": str, "stopped": bool, "reason": str}``，其中
            ``reason`` 为 ``"requested"`` / ``"already_stopping"`` /
            ``"not_running"`` 三者之一。

        Raises:
            ValueError: ``thread_id`` 非法。
            PermissionDeniedError: 缺少 thread:create 权限。
            NotFoundError: 会话不存在。
            OwnershipError: 无权访问该会话。
        """
        self._ensure_permission(principal, "thread:create")
        normalized = normalize_thread_id(thread_id)

        # WHY 所有权校验不可省：停止是「终止他人计算」的操作，若弱化为
        # 「会话在跑就能停」，任何登录用户都能打断别人的长任务。
        await self._ensure_ownership(normalized, principal)

        handle = self.run_handle(normalized)
        if handle is None:
            logger.info("会话 %s 收到停止请求：当前无运行", normalized)
            return {"thread_id": normalized, "stopped": False, "reason": "not_running"}

        if handle.stop_requested:
            logger.info("会话 %s 收到重复停止请求：忽略", normalized)
            return {"thread_id": normalized, "stopped": True, "reason": "already_stopping"}

        actor_id = principal.user_id if principal else "anonymous"
        # WHY 同步置位后再做任何 await：判重与置位之间不插入等待，
        # 单事件循环内天然原子，并发重复请求只有一次会生效并落审计。
        handle.request_stop()
        logger.info(
            "会话 %s 收到停止请求：actor=%s 已运行 %.1f 秒",
            normalized,
            actor_id,
            handle.elapsed_seconds,
        )
        await self._audit(
            event_type="run_cancelled",
            actor_id=actor_id,
            target_id=normalized,
            action="stop",
            outcome="success",
            details={"elapsed_seconds": round(handle.elapsed_seconds, 3)},
        )
        return {"thread_id": normalized, "stopped": True, "reason": "requested"}

    # ------------------------------------------------------------------ 内部

    async def _consume(
        self,
        graph: Any,
        payload: Any,
        handle: RunHandle,
    ) -> AsyncIterator[AgentEvent]:
        """消费 LangGraph 事件流并翻译成本应用事件。

        Args:
            graph: 已装配的 LangGraph 图。
            payload: 用户消息字典，或恢复执行用的 ``Command``。
            handle: 本次运行的句柄，槽位归属与停止信号都挂在它上面。

        运行槽位由调用方（``stream`` / ``resume``）在进入前占用，此处负责释放。

        Yields:
            统一事件。运行出错时先产出 ERROR 再以 DONE 收尾；
            被用户停止时以 DONE（含 ``reason: "stopped"``）收尾。
        """
        thread_id = handle.thread_id
        try:
            async for event in self._iterate(graph, payload, handle):
                yield event
        finally:
            # WHY 同步释放：客户端断开连接时这里可能正处于 GeneratorExit，
            # 任何 await 都可能破坏生成器的关闭流程。
            self.release_run_slot(thread_id)

        # WHY 在 done 之前刷新活动时间：调用方一旦停止消费，生成器剩余代码就
        # 不再执行，放在 done 之后会出现「对话已结束但列表时间没更新」的窗口。
        await self._touch(thread_id)

        # WHY 出错后仍以 DONE 收尾而不直接结束：前端依赖 DONE 复位「正在输出的
        # 那条消息」，只有 ERROR 而没有 DONE 时，下一轮回复的文本会被追加到上
        # 一轮已经出错的气泡里。让 DONE 统一表示「流已关闭」，
        # 前端就不需要在两处分别处理结束条件。
        done_payload: dict[str, Any] = {"thread_id": thread_id}
        if handle.stop_requested:
            # 用户主动停止不是错误：前端据此复位输入框并提示「已停止」，
            # 而不是把半截输出渲染成错误。
            done_payload["reason"] = "stopped"
        logger.info("会话 %s 本轮结束", thread_id)
        yield AgentEvent(AgentEventType.DONE, done_payload)

    async def _iterate(
        self,
        graph: Any,
        payload: Any,
        handle: RunHandle,
    ) -> AsyncIterator[AgentEvent]:
        """逐条翻译流增量，并负责运行期错误收敛。"""
        translator = LangGraphEventTranslator(
            tool_result_preview_limit=self._config.tool_result_preview_chars
        )

        try:
            async for mode, chunk in self._stream_graph(graph, payload, handle):
                for event in translator.feed(mode, chunk):
                    yield event

            # 流结束后冲出最后一批未发送的工具调用
            for event in translator.flush():
                yield event

        except asyncio.CancelledError:
            # WHY 单独捕获取消：客户端断开是预期行为，不应记成错误日志，
            # 但必须原样向上传播，否则 asyncio 无法完成取消流程。
            logger.info("会话 %s 运行被取消", handle.thread_id)
            raise
        except _RunStoppedError:
            # 用户主动停止不是错误：已产出的部分照常送达；未拼完的工具调用
            # 草稿刻意不冲出——参数 JSON 可能残缺，发出只会误导前端。
            logger.info("会话 %s 运行被用户停止", handle.thread_id)
        except Exception as exc:
            logger.exception("会话 %s 运行失败", handle.thread_id)
            yield AgentEvent(AgentEventType.ERROR, {"message": str(exc)})

    async def _stream_graph(
        self,
        graph: Any,
        payload: Any,
        handle: RunHandle,
    ) -> AsyncIterator[tuple[str, Any]]:
        """带停止通道地转发 ``graph.astream`` 的原始分片。

        WHY 每个分片都与停止信号竞争、而不是在分片间隙查标志位：模型调用
        与工具执行期间可能数十秒不产出任何分片，纯协作式检查会让「停止」
        长时间无响应；竞争等待让停止请求在下一个事件循环周期即生效。
        """
        astream = graph.astream(
            payload,
            config=build_runnable_config(self._config, handle.thread_id),
            stream_mode=_STREAM_MODES,
        )
        stop_task: asyncio.Task[bool] = asyncio.ensure_future(handle.cancel_event.wait())
        chunk_task: asyncio.Task[tuple[str, Any]] | None = None
        try:
            while True:
                chunk_task = asyncio.ensure_future(anext(astream))
                done, _pending = await asyncio.wait(
                    {chunk_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    # 停止请求先到：取消仍在等待的分片任务，让图执行收到
                    # CancelledError 并触发各自的资源清理（含沙箱进程回收）
                    chunk_task.cancel()
                    raise _RunStoppedError(handle.thread_id)
                try:
                    item = chunk_task.result()
                except StopAsyncIteration:
                    return
                yield item
        finally:
            # WHY 必须清理两个任务：无论正常结束、停止还是客户端断开触发的
            # 取消，都不能留下悬挂任务，否则事件循环关闭时会报
            # "Task was destroyed but it is pending"。先 cancel 再逐一 await
            # 吸收结果，避免「异常从未被取回」的告警。
            for task in (chunk_task, stop_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (chunk_task, stop_task):
                if task is not None:
                    with suppress(BaseException):
                        await task

    # -------------------------------------------------------------- 并发控制

    def _acquire_run_slot(self, thread_id: str) -> RunHandle:
        """占用该会话的运行槽位并登记运行句柄。

        WHY 必须互斥：同一会话并发发起两轮会让图状态产生竞争——两轮各自读写
        同一 thread 的检查点，后写的一方会覆盖先写一方的中间结果，表现为消息
        丢失或工具结果错配。

        Args:
            thread_id: 已规范化的会话 ID。

        Returns:
            本次运行的句柄；停止请求与运行指标都通过它传递。

        Raises:
            ThreadBusyError: 该会话已有运行中的轮次。
        """
        with self._running_guard:
            if thread_id in self._running:
                raise ThreadBusyError(thread_id)
            handle = RunHandle(
                thread_id=thread_id,
                started_at=time.monotonic(),
                cancel_event=asyncio.Event(),
            )
            self._running[thread_id] = handle
        return handle

    def release_run_slot(self, thread_id: str) -> None:
        """释放该会话的运行槽位。

        WHY 对外公开：生成器只会在被消费时通过 ``finally`` 释放槽位。若调用方
        拿到生成器后因异常未能消费（例如构造响应体时出错），槽位就再也没人释放，
        该会话会被永久判定为「运行中」。公开此方法让调用方能在这种情况下归还。

        Args:
            thread_id: 会话 ID。
        """
        with self._running_guard:
            self._running.pop(thread_id, None)

    def run_handle(self, thread_id: str) -> RunHandle | None:
        """返回指定会话的运行句柄；未在运行时为 ``None``。

        WHY 公开：运行指标（``/metrics``）与运行超时治理需要读同一份登记，
        各自维护一套集合会出现口径不一致。
        """
        with self._running_guard:
            return self._running.get(thread_id)

    def is_running(self, thread_id: str) -> bool:
        """该会话当前是否有运行中的轮次。"""
        with self._running_guard:
            return thread_id in self._running

    def running_thread_ids(self) -> tuple[str, ...]:
        """当前运行中的会话 ID 快照（供指标暴露）。"""
        with self._running_guard:
            return tuple(self._running)

    # ------------------------------------------------------------------ 元数据

    async def _record_turn(
        self,
        thread_id: str,
        *,
        title_hint: str | None,
        turn_delta: int,
        principal: Principal | None = None,
    ) -> dict[str, Any] | None:
        """把本轮对话登记到元数据表，并返回登记后的元数据。

        WHY 返回记录而不是 ``None``：``stream`` 依赖登记后的 ``owner_id``
        做「并发首条消息认领冲突」复查；此前本方法不返回值，该复查成为
        死代码，并发认领冲突会被静默漏检（Bob 可在 Alice 抢先认领的会话上
        继续运行）。

        WHY 吞掉异常：对话本身已经完成，元数据只是列表展示用的旁路信息，
        让它把一次成功的交互变成错误响应是本末倒置；失败会留下完整日志供
        排查，返回 ``None`` 让调用方跳过复查（登记失败时无从复查）。
        """
        try:
            return await self._thread_store.record_turn(
                thread_id,
                title_hint=self._build_title(title_hint),
                turn_delta=turn_delta,
                owner_id=self._owner_id(principal),
            )
        except Exception:
            logger.exception("会话活动记录失败：thread=%s", thread_id)
            return None

    async def _touch(self, thread_id: str) -> None:
        """刷新会话的最近活动时间。

        WHY 独立一次轻量更新而不是复用 ``_record_turn``：这里只需要改一个字段，
        走 UPSERT 会连带执行轮次累加与标题判断，多一次无意义的写放大。
        """
        try:
            await self._thread_store.touch(thread_id)
        except Exception:
            logger.exception("刷新会话活动时间失败：thread=%s", thread_id)

    def _build_title(self, text: str | None) -> str | None:
        """用首轮用户输入生成会话标题。

        WHY 在应用层截断而非存储层：标题长度是展示策略（由配置控制），
        存储层只保留一个防止超长文本入库的硬上限。两者共用
        ``text_utils.build_title``，只是阈值不同。
        """
        if text is None:
            return None
        return build_title(text, self._config.thread_title_max_chars) or None
