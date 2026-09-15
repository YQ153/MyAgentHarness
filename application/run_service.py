"""运行服务：发起对话、人工审批后恢复执行。

职责边界：只负责「把一次运行推进到底并产出事件」，不负责会话清单与历史
（见 ``application.thread_service.ThreadService``）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from application.errors import ThreadBusyError
from application.event_translator import LangGraphEventTranslator
from application.events import AgentEvent, AgentEventType
from application.interrupt_codec import build_resume_command
from application.runnable import build_runnable_config
from runtime.thread_store import ThreadMetaStore, normalize_thread_id
from text_utils import build_title

if TYPE_CHECKING:
    from agent.graph import AgentFactory
    from config import AppConfig

logger = logging.getLogger(__name__)

_STREAM_MODES = ["messages", "updates"]


class RunService:
    """推进一次对话运行，并把 LangGraph 的原始流翻译成统一事件。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        thread_store: ThreadMetaStore,
        graph_factory: AgentFactory,
    ) -> None:
        """构造运行服务。

        Args:
            config: 应用配置。
            thread_store: 会话元数据存储，用于登记轮次与刷新活动时间。
            graph_factory: 图工厂，提供已装配的 LangGraph 图。

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

        # WHY 用 threading.Lock 保护「运行中」集合：加解锁之间不 await，
        # 临界区极短；更重要的是释放动作必须能在 finally 里同步完成——
        # 若用 asyncio.Lock，客户端断开连接触发 GeneratorExit 时在 finally
        # 中 await 会破坏生成器的关闭流程。
        self._running: set[str] = set()
        self._running_guard = threading.Lock()

        logger.info(
            "RunService 就绪：mode=%s recursion_limit=%s",
            config.execution_mode.value,
            config.recursion_limit,
        )

    # ------------------------------------------------------------------ 运行

    async def stream(
        self,
        thread_id: str,
        user_input: str,
        *,
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
            model_name: 模型别名；``None`` 表示使用默认模型。

        Returns:
            产出统一事件的异步迭代器。

        Raises:
            ValueError: ``thread_id`` 或 ``user_input`` 非法。
            KeyError: 模型别名未注册。
            RuntimeError: 模型初始化或装配失败。
        """
        normalized = normalize_thread_id(thread_id)
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("user_input 必须是非空字符串")
        text = user_input.strip()

        # WHY 在进入图之前取图：这一步会解析模型别名并真正初始化模型，
        # 把配置与密钥错误暴露在事件流开始之前。
        graph = self._graph_factory.get(model_name)

        logger.info("会话 %s 发起运行（%d 字符）", normalized, len(text))

        # WHY 在进入图之前登记：这一刻才是会话真正诞生的时刻。放在轮次结束后
        # 登记，会让「模型初始化失败」这类早退场景下的会话凭空消失，而用户
        # 明明已经表达过意图。标题也取自这次输入——唯一「用户明确表达意图」
        # 的文本，不需要额外调用模型。
        await self._record_turn(normalized, title_hint=text, turn_delta=1)

        # WHY 在返回生成器之前占用运行槽位：占用动作若留在生成器体内，就要等到
        # 首个事件被拉取时才执行，此时响应已经开始，ThreadBusyError 只能表现为
        # 连接中断。占用成功后紧接返回生成器，中间不再有任何可能失败的语句，
        # 因此不会出现「占了槽位却没人为它收尾」。
        self._acquire_run_slot(normalized)

        payload: dict[str, Any] = {"messages": [{"role": "user", "content": text}]}
        return self._consume(graph, payload, normalized)

    async def resume(
        self,
        thread_id: str,
        decision_payload: Any,
        *,
        model_name: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """人工审批后恢复被中断的执行。

        WHY 与 ``stream`` 分开：恢复的输入是 ``Command`` 而非用户消息，
        混在一个方法里会让调用方难以判断当前处于哪种状态。

        Args:
            thread_id: 会话 ID。
            decision_payload: 审批结果，形如 ``{"decisions": [{"type": "approve"}]}``。
            model_name: 模型别名；``None`` 表示使用默认模型。

        Returns:
            产出统一事件的异步迭代器。

        Raises:
            ValueError: ``thread_id`` 非法，或审批载荷格式非法。
            KeyError: 模型别名未注册。
            RuntimeError: 模型初始化或装配失败。
        """
        normalized = normalize_thread_id(thread_id)
        # WHY 在这里就完成审批载荷校验：非法载荷必须在事件流开始之前失败，
        # 否则只能表现为连接中断，前端拿不到任何可读的失败原因。
        command = build_resume_command(decision_payload)
        graph = self._graph_factory.get(model_name)

        logger.info("会话 %s 恢复执行", normalized)

        # WHY turn_delta=0：恢复是同一轮运行的延续，重复计数会让「对话轮数」
        # 与实际用户输入次数不符；但仍然要刷新活动时间。
        await self._record_turn(normalized, title_hint=None, turn_delta=0)

        self._acquire_run_slot(normalized)
        return self._consume(graph, command, normalized)

    # ------------------------------------------------------------------ 内部

    async def _consume(
        self,
        graph: Any,
        payload: Any,
        thread_id: str,
    ) -> AsyncIterator[AgentEvent]:
        """消费 LangGraph 事件流并翻译成本应用事件。

        Args:
            graph: 已装配的 LangGraph 图。
            payload: 用户消息字典，或恢复执行用的 ``Command``。
            thread_id: 已规范化的会话 ID。

        运行槽位由调用方（``stream`` / ``resume``）在进入前占用，此处负责释放。

        Yields:
            统一事件。运行出错时会先产出 ERROR，再以 DONE 收尾。
        """
        try:
            async for event in self._iterate(graph, payload, thread_id):
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
        logger.info("会话 %s 本轮结束", thread_id)
        yield AgentEvent(AgentEventType.DONE, {"thread_id": thread_id})

    async def _iterate(
        self,
        graph: Any,
        payload: Any,
        thread_id: str,
    ) -> AsyncIterator[AgentEvent]:
        """逐条翻译流增量，并负责运行期错误收敛。"""
        translator = LangGraphEventTranslator(
            tool_result_preview_limit=self._config.tool_result_preview_chars
        )

        try:
            async for mode, chunk in graph.astream(
                payload,
                config=build_runnable_config(self._config, thread_id),
                stream_mode=_STREAM_MODES,
            ):
                for event in translator.feed(mode, chunk):
                    yield event

            # 流结束后冲出最后一批未发送的工具调用
            for event in translator.flush():
                yield event

        except asyncio.CancelledError:
            # WHY 单独捕获取消：客户端断开是预期行为，不应记成错误日志，
            # 但必须原样向上传播，否则 asyncio 无法完成取消流程。
            logger.info("会话 %s 运行被取消", thread_id)
            raise
        except Exception as exc:
            logger.exception("会话 %s 运行失败", thread_id)
            yield AgentEvent(AgentEventType.ERROR, {"message": str(exc)})

    # -------------------------------------------------------------- 并发控制

    def _acquire_run_slot(self, thread_id: str) -> None:
        """占用该会话的运行槽位。

        WHY 必须互斥：同一会话并发发起两轮会让图状态产生竞争——两轮各自读写
        同一 thread 的检查点，后写的一方会覆盖先写一方的中间结果，表现为消息
        丢失或工具结果错配。

        Args:
            thread_id: 已规范化的会话 ID。

        Raises:
            ThreadBusyError: 该会话已有运行中的轮次。
        """
        with self._running_guard:
            if thread_id in self._running:
                raise ThreadBusyError(thread_id)
            self._running.add(thread_id)

    def release_run_slot(self, thread_id: str) -> None:
        """释放该会话的运行槽位。

        WHY 对外公开：生成器只会在被消费时通过 ``finally`` 释放槽位。若调用方
        拿到生成器后因异常未能消费（例如构造响应体时出错），槽位就再也没人释放，
        该会话会被永久判定为「运行中」。公开此方法让调用方能在这种情况下归还。

        Args:
            thread_id: 会话 ID。
        """
        with self._running_guard:
            self._running.discard(thread_id)

    # ------------------------------------------------------------------ 元数据

    async def _record_turn(
        self,
        thread_id: str,
        *,
        title_hint: str | None,
        turn_delta: int,
    ) -> None:
        """把本轮对话登记到元数据表。

        WHY 吞掉异常：对话本身已经完成，元数据只是列表展示用的旁路信息，
        让它把一次成功的交互变成错误响应是本末倒置；失败会留下完整日志供排查。
        """
        try:
            await self._thread_store.record_turn(
                thread_id,
                title_hint=self._build_title(title_hint),
                turn_delta=turn_delta,
            )
        except Exception:
            logger.exception("会话活动记录失败：thread=%s", thread_id)

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
