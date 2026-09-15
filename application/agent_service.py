"""会话服务层：发起运行、流式事件归一化、中断恢复。

CLI 与 Web 共用本层。两者的差异只在于「人工决策从哪来」——CLI 读 stdin，
Web 收 HTTP 请求体——而恢复图执行的逻辑完全一致，因此下沉到此处以避免
两份实现漂移。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.types import Command

from agent.graph import get_agent, get_registry
from application.events import SSEEvent, SSEEventType
from application.interrupt_codec import build_resume_command, decode_interrupt
from runtime.store import build_store
from runtime.thread_store import ThreadMetaStore

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from config import AppConfig

logger = logging.getLogger(__name__)

_TOOL_RESULT_PREVIEW_LIMIT = 2000
"""工具结果预览的截断阈值。

WHY 截断：命令输出或大文件读取可能达数十万字符，直接推送到前端会占满带宽
并让界面卡死；同时完整结果已由模型侧落盘，前端只需摘要。
"""

_INTERRUPT_NODE = "__interrupt__"
"""LangGraph 用于承载中断信息的特殊 updates 键。"""


class AgentService:
    """管理会话生命周期与事件流。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        checkpointer: BaseCheckpointSaver,
        thread_store: ThreadMetaStore,
    ) -> None:
        if config is None:
            raise ValueError("config 不能为 None")
        if checkpointer is None:
            # WHY 强制注入而非内部自建：异步检查点的连接由上下文管理器托管，
            # 本类若自行创建就无从关闭，必然泄漏连接；而没有 checkpointer
            # 就等于没有会话恢复与中断恢复能力，属于必须满足的前置条件。
            raise ValueError("checkpointer 不能为 None")
        if thread_store is None:
            # WHY 同样强制注入：会话清单是本服务的对外能力之一，缺失时应当在
            # 装配阶段立刻失败，而不是等用户打开列表才发现始终为空。
            raise ValueError("thread_store 不能为 None")

        self._config = config
        self._checkpointer = checkpointer
        self._thread_store = thread_store
        self._store = build_store(config)
        self._registry = get_registry(config)

        logger.info(
            "AgentService 就绪：mode=%s workspace=%s",
            config.execution_mode.value,
            config.workspace,
        )

    # ------------------------------------------------------------------ 查询

    def models(self) -> list[dict[str, str]]:
        """列出可切换的模型，不含任何密钥信息。"""
        return self._registry.describe()

    def ensure_ready(self, model_name: str | None = None) -> None:
        """提前构造图，把配置与模型错误暴露在响应开始之前。

        WHY 必须有这一步：一旦进入 SSE 流，HTTP 状态码与响应头已经发出，
        此时抛出的异常只能表现为「连接被中断」，客户端拿不到任何可读信息。
        在此预先触发模型初始化，错误就能被正常的 HTTP 错误处理链路捕获。

        Raises:
            KeyError: 模型别名未注册。
            RuntimeError: 模型初始化失败（缺 Key、网络不可达等）。
        """
        get_agent(
            self._config,
            checkpointer=self._checkpointer,
            store=self._store,
            model_name=model_name,
        )

    def new_thread(self) -> str:
        """申请一个新的会话 ID。

        WHY 只发号、不落库：会话的诞生时刻被定义为「首条用户消息被接受」
        （见 ``stream()``）。若在此处登记元数据，前端每次刷新页面都会留下一行
        既无标题也无消息的空会话，CLI 启动后直接退出同样如此——空会话没有任何
        信息价值，却会永久占据会话清单。

        WHY 仍由服务端发号而不交给前端生成：会话 ID 是写入检查点的键，
        而端点当前没有鉴权，客户端可自选 ID 意味着任何人都能覆盖他人会话。
        ``uuid4`` 不可预测，这个属性必须保留。

        Returns:
            新会话的 ID；此刻数据库中尚未产生任何记录。
        """
        return uuid.uuid4().hex

    async def list_threads(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """列出会话清单，最近活动的在前。

        WHY 只读元数据表而不遍历每个会话的图状态：后者需要对每个 thread 调用
        一次 ``aget_state``，成本随会话数线性增长；而列表页只需要标题与时间。

        Args:
            limit: 返回条数，1..200。
            offset: 跳过的条数，用于分页。

        Returns:
            ``{"items": [元数据...], "total": 会话总数}``。

        Raises:
            ValueError: 分页参数非法。
            RuntimeError: 查询失败。
        """
        try:
            items = await self._thread_store.list_threads(limit=limit, offset=offset)
            total = await self._thread_store.count()
        except ValueError:
            # WHY 让参数错误原样透出：路由层需要把它映射为 400 而不是 500
            raise
        except Exception as exc:
            logger.exception("查询会话列表失败：limit=%s offset=%s", limit, offset)
            raise RuntimeError("查询会话列表失败") from exc

        logger.debug("会话列表返回 %d 条，总计 %d 条", len(items), total)
        return {"items": items, "total": total}

    async def history(self, thread_id: str) -> list[dict[str, Any]]:
        """读取会话历史，供前端刷新页面后恢复上下文。

        WHY 用 ``aget_state`` 而不用同步的 ``get_state``：异步检查点保存器
        只实现了异步接口，调用同步方法会抛 ``NotImplementedError``。

        WHY 显式返回空列表而不是抛错：不存在的 thread 在 UI 上等价于空会话，
        抛异常会让前端必须区分「无历史」与「真错误」两种情况。
        """
        graph = get_agent(
            self._config,
            checkpointer=self._checkpointer,
            store=self._store,
        )
        try:
            state = await graph.aget_state(self._runnable_config(thread_id))
        except Exception:
            logger.exception("读取会话历史失败：thread=%s", thread_id)
            return []

        if state is None:
            return []

        messages = getattr(state, "values", {}).get("messages") or []
        return [self._message_to_dict(message) for message in messages]

    async def delete_thread(self, thread_id: str) -> bool:
        """删除会话：检查点与元数据一并清理。

        WHY 直接调用 checkpointer 的删除接口：图的状态读取无法区分「空会话」
        与「不存在」，而 ``update_state`` 只会追加写入，始终删不掉历史记录。

        Returns:
            该会话是否确实登记过并被移除。判定以元数据登记为准——对从未发言过的
            ID 返回 ``False``（它本来就不存在），避免响应里的 ``deleted``
            把「什么都没删到」报成成功。

        Raises:
            ValueError: ``thread_id`` 为空。
        """
        if not thread_id or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")
        normalized = thread_id.strip()

        meta_deleted = False
        try:
            meta_deleted = await self._thread_store.delete(normalized)
        except Exception:
            # WHY 不向上抛：元数据只用于清单展示，让它把一次删除操作变成 500 会
            # 误导调用方以为整个操作失败；检查点仍然应该继续清理。
            logger.exception("删除会话元数据失败：thread=%s", normalized)

        checkpoint_deleted = await self._delete_checkpoints(normalized)

        # WHY 返回值以元数据为准：会话清单读的就是 thread_meta，它的增删才决定
        # 会话在界面上「是否存在」。而 checkpointer 的 adelete_thread 对不存在的
        # 会话是静默成功的 no-op，无法用来判断会话是否真的存在过——若以它为准，
        # 删除一个从未发言过的 ID 也会返回「已删除」。
        if meta_deleted:
            logger.info(
                "会话已删除：thread=%s checkpoint=%s", normalized, checkpoint_deleted
            )
        else:
            logger.warning(
                "会话未登记，视为不存在：thread=%s checkpoint=%s",
                normalized,
                checkpoint_deleted,
            )
        return meta_deleted

    async def _delete_checkpoints(self, thread_id: str) -> bool:
        """删除该会话的全部检查点。

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

    # ------------------------------------------------------------------ 运行

    async def stream(
        self,
        thread_id: str,
        user_input: str,
        *,
        model_name: str | None = None,
    ) -> AsyncIterator[SSEEvent]:
        """发起一轮对话并产出归一化事件。"""
        if not thread_id or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")
        if not user_input or not user_input.strip():
            raise ValueError("user_input 不能为空")

        logger.info("会话 %s 发起运行（%d 字符）", thread_id, len(user_input))

        # WHY 在进入图之前登记：这一刻才是会话真正诞生的时刻。放在轮次结束后
        # 登记，会让「模型初始化失败」这类早退场景下的会话凭空消失，而用户
        # 明明已经表达过意图。标题也取自这次输入——唯一「用户明确表达意图」
        # 的文本，不需要额外调用模型。
        await self._record_turn(thread_id, title_hint=user_input, turn_delta=1)

        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": user_input}]
        }
        async for event in self._consume(payload, thread_id, model_name):
            yield event

    async def resume(
        self,
        thread_id: str,
        decision_payload: Any,
        *,
        model_name: str | None = None,
    ) -> AsyncIterator[SSEEvent]:
        """人工审批后恢复被中断的执行。

        WHY 与 ``stream`` 分开：恢复的输入是 ``Command`` 而非用户消息，
        混在一个方法里会让调用方难以判断当前处于哪种状态。
        """
        if not thread_id or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")

        command = build_resume_command(decision_payload)
        logger.info("会话 %s 恢复执行", thread_id)

        # WHY turn_delta=0：恢复是同一轮运行的延续，重复计数会让「对话轮数」
        # 与实际用户输入次数不符；但仍然要刷新活动时间。
        await self._record_turn(thread_id, title_hint=None, turn_delta=0)

        async for event in self._consume(command, thread_id, model_name):
            yield event

    # ------------------------------------------------------------------ 内部

    def _runnable_config(self, thread_id: str) -> dict[str, Any]:
        """构造 LangGraph 运行配置。

        WHY 必须设 recursion_limit：通用 Agent 的长任务可能触发深层循环，
        默认值过小会导致任务在中途被硬性终止。
        """
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self._config.recursion_limit,
        }

    async def _consume(
        self,
        payload: Any,
        thread_id: str,
        model_name: str | None,
    ) -> AsyncIterator[SSEEvent]:
        """消费 LangGraph 事件流并翻译成本应用事件。

        Args:
            payload: 用户消息字典，或恢复执行用的 ``Command``。
            thread_id: 会话 ID。
            model_name: 模型别名；``None`` 表示用默认模型。
        """
        graph = get_agent(
            self._config,
            checkpointer=self._checkpointer,
            store=self._store,
            model_name=model_name,
        )
        cfg = self._runnable_config(thread_id)

        # index -> 累积中的工具调用
        pending_tool_calls: dict[int, dict[str, Any]] = {}
        last_node: str | None = None

        try:
            async for mode, chunk in graph.astream(
                payload,
                config=cfg,
                stream_mode=["messages", "updates"],
            ):
                if mode == "messages":
                    message, metadata = chunk
                    node = (metadata or {}).get("langgraph_node")

                    # WHY 以节点切换作为工具调用参数拼接完成的信号：
                    # 工具参数是分片到达的，而模型节点一旦结束就转为工具节点，
                    # 这是唯一可靠的 flush 时机。
                    if node != last_node:
                        for event in self._flush_tool_calls(pending_tool_calls):
                            yield event
                        last_node = node

                    for event in self._handle_message(message, pending_tool_calls):
                        yield event

                elif mode == "updates":
                    for event in self._handle_updates(chunk, pending_tool_calls):
                        yield event

            # 流结束后冲出最后一批未发送的工具调用
            for event in self._flush_tool_calls(pending_tool_calls):
                yield event

        except Exception as exc:
            logger.exception("会话 %s 运行失败", thread_id)
            yield SSEEvent(SSEEventType.ERROR, {"message": str(exc)})
            return

        # WHY 在 done 之前刷新活动时间：调用方一旦停止消费，生成器剩余代码就
        # 不再执行，放在 done 之后会出现「对话已结束但列表时间没更新」的窗口。
        await self._record_turn(thread_id, title_hint=None, turn_delta=0)

        logger.info("会话 %s 本轮结束", thread_id)
        yield SSEEvent(SSEEventType.DONE, {"thread_id": thread_id})

    def _handle_message(
        self,
        message: Any,
        pending_tool_calls: dict[int, dict[str, Any]],
    ) -> list[SSEEvent]:
        """处理 messages 流：文本增量、工具调用分片、工具结果。"""
        events: list[SSEEvent] = []

        if isinstance(message, AIMessageChunk):
            text = getattr(message, "text", None)
            if text:
                events.append(SSEEvent(SSEEventType.TOKEN, {"text": text}))

            for fragment in getattr(message, "tool_call_chunks", None) or []:
                index = fragment.get("index")
                if index is None:
                    continue
                draft = pending_tool_calls.setdefault(
                    index, {"name": None, "index": index, "args": ""}
                )
                if fragment.get("name"):
                    draft["name"] = fragment["name"]
                # args 是 JSON 字符串的分片，直接拼接后再整体反序列化
                if fragment.get("args"):
                    draft["args"] += fragment["args"]
            return events

        if isinstance(message, ToolMessage):
            # 工具结果出现意味着前面的调用已完整，先冲出发出去
            events.extend(self._flush_tool_calls(pending_tool_calls))
            content = message.content
            if isinstance(content, str):
                preview = content[:_TOOL_RESULT_PREVIEW_LIMIT]
                truncated = len(content) > _TOOL_RESULT_PREVIEW_LIMIT
            else:
                preview = str(content)[:_TOOL_RESULT_PREVIEW_LIMIT]
                truncated = True
            events.append(
                SSEEvent(
                    SSEEventType.TOOL_RESULT,
                    {
                        "name": getattr(message, "name", "") or "",
                        "status": getattr(message, "status", "") or "",
                        "preview": preview,
                        "truncated": truncated,
                    },
                )
            )
            return events

        return events

    def _handle_updates(
        self,
        chunk: Any,
        pending_tool_calls: dict[int, dict[str, Any]],
    ) -> list[SSEEvent]:
        """处理 updates 流：中断请求与待办快照。"""
        events: list[SSEEvent] = []
        if not isinstance(chunk, dict):
            return events

        pending = decode_interrupt(chunk)
        if pending is not None:
            events.extend(self._flush_tool_calls(pending_tool_calls))
            events.append(SSEEvent(SSEEventType.INTERRUPT, pending.to_payload()))
            return events

        if _INTERRUPT_NODE in chunk:
            return events

        for node_name, update in chunk.items():
            if not isinstance(update, dict):
                continue
            events.append(SSEEvent(SSEEventType.STEP, {"node": node_name}))
            todos = update.get("todos")
            if todos is not None:
                events.append(SSEEvent(SSEEventType.TODOS, {"items": list(todos)}))

        return events

    def _flush_tool_calls(
        self,
        pending_tool_calls: dict[int, dict[str, Any]],
    ) -> list[SSEEvent]:
        """把累积的工具调用分片转成完整事件并清空缓冲。"""
        if not pending_tool_calls:
            return []

        events: list[SSEEvent] = []
        for index in sorted(pending_tool_calls):
            draft = pending_tool_calls[index]
            raw_args = draft.get("args") or ""
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                # 参数不是合法 JSON 时保留原文，便于前端原样展示排查
                args = {"__raw__": raw_args}
            events.append(
                SSEEvent(
                    SSEEventType.TOOL_CALL,
                    {
                        "index": index,
                        "name": draft.get("name") or "unknown",
                        "args": args,
                    },
                )
            )

        pending_tool_calls.clear()
        return events

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

    def _build_title(self, text: str | None) -> str | None:
        """用首轮用户输入生成会话标题。

        WHY 在应用层截断而非存储层：标题长度是展示策略（由配置控制），
        存储层只保留一个防止超长文本入库的硬上限，两者职责不同。
        """
        if not text:
            return None

        # WHY 折叠空白：标题会渲染在单行列表项里，保留换行会撑破布局
        collapsed = " ".join(str(text).split())
        if not collapsed:
            return None

        limit = self._config.thread_title_max_chars
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[:limit] + "…"

    @staticmethod
    def _message_to_dict(message: Any) -> dict[str, Any]:
        """把 LangChain 消息转成前端可直接渲染的字典。"""
        role = getattr(message, "type", "") or ""
        tool_calls = getattr(message, "tool_calls", None) or []
        return {
            "role": role,
            "content": message.content if isinstance(message.content, str) else str(message.content),
            "name": getattr(message, "name", "") or "",
            "tool_calls": [
                {
                    "name": call.get("name", ""),
                    "args": call.get("args", {}),
                }
                for call in tool_calls
            ],
        }
