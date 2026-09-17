"""LangGraph 事件流 → 应用事件的翻译器。

WHY 独立成类：这是一段**有状态**的解析逻辑（工具调用的参数按分片到达，需要
跨 chunk 累积中间结果），与「会话编排」没有任何关系。此前它内联在会话服务里，
导致三个后果：

1. 无法单独测试——要验证「参数分片拼接正确」必须 mock 掉整个图、checkpointer、
   元数据存储与模型初始化；独立之后喂一串 chunk 即可断言产出。
2. 框架细节泄漏到应用层——``AIMessageChunk.tool_call_chunks``、
   ``metadata["langgraph_node"]`` 都是 LangGraph 的私有约定。
3. LangGraph 调整 chunk 结构时，要改动的是会话编排类，违反了变更隔离。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessageChunk, ToolMessage

from application.events import AgentEvent, AgentEventType
from application.interrupt_codec import INTERRUPT_NODE, decode_interrupt
from application.usage import TokenUsage, UsageAccumulator

logger = logging.getLogger(__name__)

_MESSAGES_MODE = "messages"
"""`graph.astream` 的 messages 流标识。"""

_UPDATES_MODE = "updates"
"""`graph.astream` 的 updates 流标识。"""


class LangGraphEventTranslator:
    """把 LangGraph 的两种流增量翻译成统一的应用事件。

    用法：对 ``astream`` 产出的每个 ``(mode, chunk)`` 调用 :meth:`feed`，
    流结束后调用 :meth:`flush` 取出最后一批未拼完的工具调用。
    """

    def __init__(self, *, tool_result_preview_limit: int) -> None:
        """构造翻译器。

        Args:
            tool_result_preview_limit: 工具结果预览的字符上限。

        Raises:
            ValueError: ``tool_result_preview_limit`` 不是正整数。
        """
        if (
            not isinstance(tool_result_preview_limit, int)
            or isinstance(tool_result_preview_limit, bool)
        ):
            raise ValueError(
                f"tool_result_preview_limit 必须是整数，实际："
                f"{type(tool_result_preview_limit).__name__}"
            )
        if tool_result_preview_limit < 1:
            raise ValueError(
                f"tool_result_preview_limit 必须 >= 1，实际：{tool_result_preview_limit}"
            )

        self._preview_limit = tool_result_preview_limit
        self._pending_tool_calls: dict[int, dict[str, Any]] = {}
        self._last_node: str | None = None
        self._usage = UsageAccumulator()

    # ---------------------------------------------------------------- 输入

    @property
    def usage(self) -> TokenUsage:
        """本轮累计的 token 用量；未取到任何用量时为零值。

        WHY 由翻译器持有累计器：用量随消息分片到达，与「工具参数分片」是
        同一条流上的两个派生量，放在同一处才能保证「流走完就能读到用量」，
        而不必让调用方再去遍历一次事件。
        """
        return self._usage.total

    @property
    def has_usage(self) -> bool:
        """是否曾从流中取到用量字段。"""
        return not self._usage.empty

    def feed(self, mode: str, chunk: Any) -> list[AgentEvent]:
        """消费一个流增量。

        Args:
            mode: 流模式，``"messages"`` 或 ``"updates"``。
            chunk: 该模式下的原始载荷。

        Returns:
            本次增量翻译出的事件列表；无可发布事件时为空列表。
        """
        if mode == _MESSAGES_MODE:
            return self._on_messages(chunk)
        if mode == _UPDATES_MODE:
            return self._on_updates(chunk)

        logger.debug("忽略未识别的 stream_mode：%r", mode)
        return []

    def flush(self) -> list[AgentEvent]:
        """把累积中的工具调用转成完整事件并清空缓冲。

        Returns:
            待发送的工具调用事件；缓冲为空时返回空列表。
        """
        if not self._pending_tool_calls:
            return []

        events: list[AgentEvent] = []
        for index in sorted(self._pending_tool_calls):
            draft = self._pending_tool_calls[index]
            raw_args = draft.get("args") or ""
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                # WHY 保留原文而非丢弃：非法 JSON 往往意味着模型输出了畸形参数，
                # 原样透出才能让前端与日志里看到问题所在。
                logger.warning("工具参数不是合法 JSON，已保留原文：name=%s", draft.get("name"))
                args = {"__raw__": raw_args}
            events.append(
                AgentEvent(
                    AgentEventType.TOOL_CALL,
                    {
                        "index": index,
                        "name": draft.get("name") or "unknown",
                        "args": args,
                    },
                )
            )

        self._pending_tool_calls.clear()
        return events

    # ---------------------------------------------------------------- 内部

    def _on_messages(self, chunk: Any) -> list[AgentEvent]:
        """处理 messages 流：文本增量、工具调用分片、工具结果。"""
        if not isinstance(chunk, (tuple, list)) or len(chunk) != 2:
            logger.warning("messages 流载荷结构异常，已跳过：%r", type(chunk).__name__)
            return []

        message, metadata = chunk
        node = metadata.get("langgraph_node") if isinstance(metadata, dict) else None

        events: list[AgentEvent] = []

        # WHY 以节点切换作为工具调用参数拼接完成的信号：工具参数是分片到达的，
        # 而模型节点一旦结束就转为工具节点，这是唯一可靠的 flush 时机。
        if node != self._last_node:
            events.extend(self.flush())
            self._last_node = node

        events.extend(self._on_message(message))
        return events

    def _on_message(self, message: Any) -> list[AgentEvent]:
        """翻译单条 LangChain 消息。"""
        if isinstance(message, AIMessageChunk):
            return self._on_ai_chunk(message)
        if isinstance(message, ToolMessage):
            return self._on_tool_message(message)
        return []

    def _on_ai_chunk(self, message: AIMessageChunk) -> list[AgentEvent]:
        """翻译模型输出分片：文本增量、工具调用参数分片与 token 用量。"""
        events: list[AgentEvent] = []

        # WHY 每个分片都尝试读用量：provider 上报位置不统一（有的只在最后一
        # 片给全量，有的每片给累计值），逐片交给累计器判定比挑某一片可靠。
        if not self._usage.add_raw(message):
            logger.debug("模型分片未携带用量字段：node=%s", self._last_node)

        text = getattr(message, "text", None)
        if text:
            events.append(AgentEvent(AgentEventType.TOKEN, {"text": text}))

        for fragment in getattr(message, "tool_call_chunks", None) or []:
            if not isinstance(fragment, dict):
                continue
            index = fragment.get("index")
            if index is None:
                continue

            draft = self._pending_tool_calls.setdefault(
                index, {"name": None, "index": index, "args": ""}
            )
            if fragment.get("name"):
                draft["name"] = fragment["name"]
            # args 是 JSON 字符串的分片，直接拼接后再整体反序列化
            if fragment.get("args"):
                draft["args"] += fragment["args"]

        return events

    def _on_tool_message(self, message: ToolMessage) -> list[AgentEvent]:
        """翻译工具结果。"""
        # 工具结果出现意味着前面的调用已完整，先冲出发出去
        events: list[AgentEvent] = self.flush()

        content = message.content
        if isinstance(content, str):
            preview = content[: self._preview_limit]
            truncated = len(content) > self._preview_limit
        else:
            # WHY 非字符串一律标为已截断：无法确定其文本化后的完整长度，
            # 宁可保守标记，也不要让前端误以为拿到了全部内容。
            preview = str(content)[: self._preview_limit]
            truncated = True

        events.append(
            AgentEvent(
                AgentEventType.TOOL_RESULT,
                {
                    "name": getattr(message, "name", "") or "",
                    "status": getattr(message, "status", "") or "",
                    "preview": preview,
                    "truncated": truncated,
                },
            )
        )
        return events

    def _on_updates(self, chunk: Any) -> list[AgentEvent]:
        """处理 updates 流：中断请求、节点进度与待办快照。"""
        if not isinstance(chunk, dict):
            return []

        pending = decode_interrupt(chunk)
        if pending is not None:
            # 中断前先把未冲出的工具调用发出，前端才知道要审批的是哪个调用
            events: list[AgentEvent] = self.flush()
            events.append(AgentEvent(AgentEventType.INTERRUPT, pending.to_payload()))
            return events

        if INTERRUPT_NODE in chunk:
            # 中断载荷存在但解析不出有效内容，交由 decode_interrupt 的告警覆盖
            return []

        events = []
        for node_name, update in chunk.items():
            if not isinstance(update, dict):
                continue
            events.append(AgentEvent(AgentEventType.STEP, {"node": node_name}))
            todos = update.get("todos")
            if todos is not None:
                events.append(AgentEvent(AgentEventType.TODOS, {"items": list(todos)}))

        return events
