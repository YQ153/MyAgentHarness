"""运行服务：发起对话、人工审批后恢复执行。

职责边界：只负责「把一次运行推进到底并产出事件」，不负责会话清单与历史
（见 ``application.thread_service.ThreadService``）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent.run_context import ANONYMOUS_USER_ID, AgentRunContext
from application.audit_context import audit_client_info
from application.dto import GovernanceReport
from application.errors import (
    InterruptExpiredError,
    NotFoundError,
    OwnershipError,
    PermissionDeniedError,
    ThreadBusyError,
)
from application.event_translator import LangGraphEventTranslator
from application.events import AgentEvent, AgentEventType
from application.interrupt_codec import build_resume_command
from application.ownership import ensure_thread_access
from application.principal import Principal
from application.runnable import build_runnable_config
from application.usage import TokenUsage
from runtime.audit_store import AuditStore
from runtime.execution_registry import abort_scope, bound_scope
from runtime.thread_store import ThreadMetaStore
from runtime.tool_outputs import prune_tool_outputs, tool_output_path, write_tool_output
from runtime.usage_store import UsageStore
from runtime.workspace_files import to_virtual_path
from text_utils import build_title
from thread_utils import normalize_thread_id


def _role_of(message: Any) -> str:
    """把一条图消息的角色归一成 user / assistant / tool / system。

    WHY 不直接用 LangChain 的 ``type``：它以 ``human`` / ``ai`` 命名，而应用层与前端
    一直用 ``user`` / ``assistant``。两套叫法在消息筛选处混用会静默漏判（例如把
    ``human`` 当成未知角色），故在入口处一次性归一。
    """
    kind = getattr(message, "type", "") or ""
    return {"human": "user", "ai": "assistant"}.get(kind, kind or "other")


def _message_text(message: Any) -> str:
    """取消息正文；正文是多段内容时拼接其中的文本段。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


def _last_user_index(messages: list[Any]) -> int | None:
    """最后一条用户消息的下标；一条都没有时返回 ``None``。"""
    for index in range(len(messages) - 1, -1, -1):
        if _role_of(messages[index]) == "user":
            return index
    return None


def _user_turn_number(messages: list[Any], index: int) -> int:
    """下标 ``index`` 是第几条用户消息（1 基），用于给分支起一个人能看懂的名字。"""
    return sum(1 for message in messages[: index + 1] if _role_of(message) == "user")


def _same_message_chain(left: list[Any], right: list[Any]) -> bool:
    """判断两段消息是否同一条历史链。

    WHY 优先比 id 而不是正文：编辑过的消息正文不同，但这里要确认的是「这是同一段
    历史」而不是「文字一样」——正文比较会把两条内容恰好相同的分支判成同一条。
    id 缺失（手写 dict 输入的情形）时退回正文比较。
    """
    if len(left) != len(right):
        return False
    for one, other in zip(left, right):
        left_id = getattr(one, "id", None)
        right_id = getattr(other, "id", None)
        if left_id and right_id:
            if left_id != right_id:
                return False
            continue
        if _message_text(one) != _message_text(other):
            return False
    return True


if TYPE_CHECKING:
    from agent.graph import AgentFactory
    from application.tool_catalog import ToolCatalog
    from config import AppConfig

logger = logging.getLogger(__name__)

_STREAM_MODES = ["messages", "updates"]

_TOOL_ARGS_PREVIEW_CHARS = 500
"""工具参数写入审计时的字符上限。

WHY 需要截断：工具参数可以是整篇文件内容或长命令行，原样落库会让审计表
的体积由「调用次数」变成「调用次数 × 参数体积」；而审计要回答的是「调了
什么工具、带什么意图」，前若干字符已经足够定位。
"""


def _preview_args(args: Any) -> str:
    """把工具参数压成一段可入库的短文本。

    Args:
        args: 翻译层给出的参数对象（通常是 dict，非法 JSON 时为 ``{"__raw__": ...}``）。

    Returns:
        截断后的 JSON 文本；无法序列化时退化为 ``repr``，绝不抛异常——
        审计是旁路职责，不能因为参数里有不可序列化的对象而中断本轮运行。
    """
    try:
        text = json.dumps(args, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        logger.debug("工具参数无法序列化为 JSON，退化用 repr", exc_info=True)
        text = repr(args)
    if len(text) > _TOOL_ARGS_PREVIEW_CHARS:
        return text[:_TOOL_ARGS_PREVIEW_CHARS] + "…"
    return text

STOP_REASON_STOPPED = "stopped"
"""DONE 事件的停止原因：用户主动停止。"""

STOP_REASON_TIMEOUT = "timeout"
"""DONE 事件的停止原因：运行超过 ``run_max_seconds`` 被治理协程强制取消。

WHY 与 ``stopped`` 区分：两者对前端都是「流已关闭、内容不完整」，但责任方
不同——一个是用户按了停止，一个是系统判定超时；混成一个值会让用户在没有
任何操作的情况下看到「已停止」，从而误判界面出了 bug。
"""

SYSTEM_ACTOR = "system"
"""后台治理动作的审计主体标识。

WHY 不用空串：审计表的 ``actor_id`` 为空表示「未知」，而后台治理确实是
系统做出的决定，二者在事后追溯时含义完全不同。
"""


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
    model_name: str | None = None
    """本轮使用的模型别名；``None`` 表示默认模型（落用量时按配置解析）。"""
    owner_id: str = ""
    """会话所有者；用量记录按它聚合，认证关闭时为空串。"""
    actor_id: str = ""
    """发起本轮运行的主体标识；工具审计按它归因，认证关闭时为 ``anonymous``。"""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    """本轮发生的工具调用记录。

    WHY 挂在句柄上而不是服务上：一轮运行的工具调用天然属于这一轮，按
    thread_id 另建一份字典会多出一套「运行结束即清理」的生命周期管理，
    而句柄本来就随运行释放。

    WHY 用可变列表而不是 frozen 语义：记录是在同步热路径（``_track_event``）
    里逐条追加的，落库则在流结束后统一进行；可变容器是这一写多读场景下
    唯一不需要加锁的形态。
    """
    stop_reason: str | None = None
    """停止原因；``None`` 表示尚未收到停止请求，取值见模块级常量。"""
    fork_checkpoint: str = ""
    """本次运行的分叉起点检查点 id；空串表示接着当前分支的头跑。"""

    @property
    def stop_requested(self) -> bool:
        """是否已收到停止请求。"""
        return self.cancel_event.is_set()

    @property
    def elapsed_seconds(self) -> float:
        """已运行时长（秒）。"""
        return time.monotonic() - self.started_at

    @property
    def memory_owner(self) -> str:
        """本轮运行长期记忆的归属主体。

        WHY 直接复用 ``owner_id``：记忆是「这个用户的偏好」，与会话归属同源，
        另存一份必然出现两者漂移。认证关闭时 ``owner_id`` 为空串，这里统一
        落到匿名标识——否则空串会被当成一个独立命名空间，让同一台机器上
        「CLI 写的记忆 Web 读不到」。
        """
        return self.owner_id or ANONYMOUS_USER_ID

    def request_stop(self, reason: str = STOP_REASON_STOPPED) -> None:
        """请求停止本次运行；重复调用时首次的原因生效。

        WHY 保留首次原因：超时强制取消之后用户再点停止（或反过来），
        先到达的那个才是运行的真实终止原因；覆盖它会让审计与前端
        「已超时」的结论被后来的操作改写。

        WHY 用 ``object.__setattr__`` 而不是把整个句柄改成可变：句柄的
        ``thread_id`` / ``started_at`` 一旦可写，运行登记就失去了可信度；
        这里只为「一次性记录原因」破一个口子，比整体降级为可变更安全。

        Args:
            reason: 停止原因；取 ``STOP_REASON_STOPPED`` 或
                ``STOP_REASON_TIMEOUT``。
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空字符串")
        if self.stop_reason is None:
            object.__setattr__(self, "stop_reason", reason.strip())
        self.cancel_event.set()


@dataclass(eq=False)
class ToolCallRecord:
    """一次工具调用的审计草稿。

    WHY 单独成类：工具调用的开始（TOOL_CALL）与结束（TOOL_RESULT）是两个
    不同的事件，耗时只有把它们配对后才能算出来；用一个记录对象承载这对
    状态，比在两个字典里分别记时间戳更容易保证不漏、不串。
    """

    name: str
    started_at: float
    args_preview: str = ""
    status: str = ""
    """工具结果的 status；空串表示运行结束前都未收到结果。"""
    elapsed_ms: int | None = None


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
        usage_store: UsageStore | None = None,
        tool_catalog: ToolCatalog | None = None,
    ) -> None:
        """构造运行服务。

        Args:
            config: 应用配置。
            thread_store: 会话元数据存储，用于登记轮次与刷新活动时间。
            graph_factory: 图工厂，提供已装配的 LangGraph 图。
            audit_store: 审计日志存储，可选。
            usage_store: Token 用量存储，可选；为 ``None`` 时不记录用量。
            tool_catalog: 工具目录，用于审计时标注工具来源；``None`` 时
                工具审计仍会记录，但来源字段为 ``None``。

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
        self._usage_store = usage_store
        self._tool_catalog = tool_catalog

        # WHY 用 threading.Lock 保护「运行中」登记表：加解锁之间不 await，
        # 临界区极短；更重要的是释放动作必须能在 finally 里同步完成——
        # 若用 asyncio.Lock，客户端断开连接触发 GeneratorExit 时在 finally
        # 中 await 会破坏生成器的关闭流程。
        self._running: dict[str, RunHandle] = {}
        self._running_guard = threading.Lock()

        # WHY 累计运行数与 HITL 挂起登记与运行登记表共用一把锁：三者都是
        # 「本次运行的即时状态」，若各自加锁，指标采集会读到互相矛盾的组合
        # （例如累计运行数已加一，但槽位尚未登记）。
        self._started_runs = 0
        self._hitl_pending: dict[str, float] = {}
        """会话 ID → 挂起登记时刻（``time.monotonic``），用于 TTL 判定。"""
        self._hitl_expired: dict[str, float] = {}
        """会话 ID → 被判定超期的时刻；用于拒绝过期审批与指标展示。

        WHY 与 ``_hitl_pending`` 分开存：过期是「曾经挂起且已作废」的历史事实，
        而挂起是当下状态。合成一个字典就要用哨兵值区分二者，届时每个读取点
        都要记得判断哨兵——漏一处就会出现「已过期的审批被放行」。
        """
        self._timed_out_runs = 0
        """进程启动以来被运行超时强制取消的运行数。"""
        self._expired_hitl = 0
        """进程启动以来被判定超期作废的审批挂起数。"""

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

        判定规则统一在 ``application.ownership.ensure_thread_access``，
        与会话服务、用量服务共用同一份语义。

        Args:
            allow_claim: 允许在未登记时「认领」该会话（用于 ``stream`` 首条消息场景）。

        Raises:
            NotFoundError: 会话不存在且 ``allow_claim=False``。
            OwnershipError: 无权访问。
        """
        record = await self._thread_store.get(thread_id)
        return ensure_thread_access(
            record,
            thread_id,
            self._config,
            principal,
            allow_claim=allow_claim,
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
        """记录一条审计事件；缺失审计存储或运行时异常均不影响业务。

        WHY 在此读取请求上下文：IP/UA 是审计的定位信息，由接口层的中间件
        写入 ``contextvars``。放在这里统一读取，调用点就不必逐个透传请求
        信息，也不会因为某个调用点漏传而产出无来源的记录。
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
            # WHY 审计失败不上抛：审计是旁路职责，把一次成功的业务操作变成
            # 500 会让「日志库满」演变成全站故障；失败已留完整日志供告警。
            logger.exception("审计事件写入失败：event_type=%s actor=%s", event_type, actor_id)

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

        # WHY 新一轮用户输入会作废此前悬着的审批请求：用户既已改口，那个
        # 审批卡就不再代表当前意图；留着它只会让「待审批数」无限增长。
        self.clear_hitl_pending(normalized)

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
        handle = self._acquire_run_slot(
            normalized,
            model_name=model_name,
            owner_id=self._owner_id(principal),
            actor_id=actor_id,
        )

        payload: dict[str, Any] = {"messages": [{"role": "user", "content": text}]}
        return self._consume(graph, payload, handle)

    # ------------------------------------------------------------ 编辑与分叉

    async def regenerate(
        self,
        thread_id: str,
        *,
        principal: Principal | None = None,
        model_name: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """重新生成最后一轮助手回复。

        WHY 与编辑共用同一条机制：两者都是「从某个历史检查点分叉，再用一段文本跑一次」，
        差别只在分叉点与新文本从哪来。分成两套实现会让权限、并发、分支登记、用量归属
        各写一遍，而它们迟早分叉；合成一条路径则这些语义只存在一处。

        WHY 是分叉而不是原地重跑：旧回复所在的路径原样保留，用户不满意时还能切回去
        比对；这也让「成本不因重新生成而消失」自然成立——旧分支的用量记录一条都没删。

        Returns:
            产出统一事件的异步迭代器。

        Raises:
            ValueError: ``thread_id`` 非法，或该会话还没有可用的用户消息。
            PermissionDeniedError: 缺少 thread:create 权限。
            NotFoundError: 会话或分支不存在。
            OwnershipError: 无权访问该会话。
            ThreadBusyError: 该会话已有运行中的轮次。
        """
        normalized = normalize_thread_id(thread_id)
        messages = await self._branch_messages(normalized, principal)

        last_user = _last_user_index(messages)
        if last_user is None:
            raise ValueError("该会话还没有用户消息，无法重新生成")
        text = _message_text(messages[last_user]).strip()
        if not text:
            raise ValueError("最后一条用户消息没有文本内容，无法重新生成")

        return await self._fork_and_run(
            normalized,
            text,
            messages=messages,
            target_index=last_user,
            origin="regenerate",
            label="重新生成",
            principal=principal,
            model_name=model_name,
        )

    async def edit(
        self,
        thread_id: str,
        message_index: int,
        content: str,
        *,
        principal: Principal | None = None,
        model_name: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """改写指定下标的用户消息，并从该点分叉重跑。

        WHY 用下标而不是消息 id 定位：历史消息 DTO 一直不对外暴露 id，为编辑单独加一个
        字段会让前端必须先从两处对上号；下标在「某条分支的消息列表」内是确定的，而编辑
        本来就必须先看到那份列表。

        Raises:
            ValueError: ``thread_id`` 非法、下标越界、目标不是用户消息、或新文本为空。
            PermissionDeniedError: 缺少 thread:create 权限。
            NotFoundError: 会话或分支不存在。
            OwnershipError: 无权访问该会话。
            ThreadBusyError: 该会话已有运行中的轮次。
        """
        normalized = normalize_thread_id(thread_id)
        if not isinstance(message_index, int) or isinstance(message_index, bool):
            raise ValueError(f"message_index 必须是整数，实际：{type(message_index).__name__}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content 必须是非空字符串")
        text = content.strip()

        messages = await self._branch_messages(normalized, principal)
        if message_index >= len(messages):
            raise ValueError(f"message_index 越界（{message_index} >= {len(messages)}）")
        if _role_of(messages[message_index]) != "user":
            raise ValueError("只能编辑用户消息")

        turn = _user_turn_number(messages, message_index)
        return await self._fork_and_run(
            normalized,
            text,
            messages=messages,
            target_index=message_index,
            origin="edit",
            label=f"编辑第 {turn} 轮",
            principal=principal,
            model_name=model_name,
        )

    async def _fork_and_run(
        self,
        thread_id: str,
        text: str,
        *,
        messages: list[Any],
        target_index: int,
        origin: str,
        label: str,
        principal: Principal | None,
        model_name: str | None,
    ) -> AsyncIterator[AgentEvent]:
        """从「目标消息出现之前」的检查点分叉，并用 ``text`` 跑一轮。

        Raises:
            ValueError: 找不到分叉点（历史已被清理）。
            ThreadBusyError: 该会话已有运行中的轮次。
        """
        # WHY 先取图再登记分支：解析模型别名与初始化模型可能失败，那属于「什么都没
        # 发生」；若先写了分支再失败，分支清单里会多出一条没有任何内容的分支。
        graph = self._graph_factory.get(model_name)
        actor_id = principal.user_id if principal else "anonymous"

        fork_checkpoint = await self._find_fork_checkpoint(
            graph, thread_id, messages, target_index
        )

        current = await self._thread_store.current_branch(thread_id)
        # WHY 冻结必须在登记新分支之前：反过来一旦中途失败，旧分支的头就再也回不来，
        # 而新分支已经把自己设成当前——分支清单从此无法还原成操作前的样子。
        live_head = await self._live_head(graph, thread_id)
        await self._thread_store.set_branch_head(thread_id, current, live_head)

        branch_id = uuid.uuid4().hex
        await self._thread_store.upsert_branch(
            thread_id,
            branch_id,
            parent_branch_id=current,
            origin=origin,
            label=label,
        )
        await self._thread_store.set_current_branch(thread_id, branch_id)

        # WHY 与新一轮输入同样作废悬着的审批：用户既已改口，那张审批卡就不再代表
        # 当前意图，留着只会让「待审批数」无限增长。
        self.clear_hitl_pending(thread_id)
        # WHY 只刷新活动时间而不加轮次：轮次记的是「用户发起了几轮」，编辑与重新生成
        # 都没有新增一次用户发起；把它们算进去会让清单上的轮数凭空增长。
        await self._thread_store.touch(thread_id)
        await self._audit(
            event_type="thread_branch",
            actor_id=actor_id,
            target_id=thread_id,
            action=origin,
            outcome="success",
            details={
                "branch_id": branch_id,
                "parent_branch_id": current,
                "label": label,
                "message_index": target_index,
            },
        )

        handle = self._acquire_run_slot(
            thread_id,
            model_name=model_name,
            owner_id=self._owner_id(principal),
            actor_id=actor_id,
            fork_checkpoint=fork_checkpoint,
        )

        payload: dict[str, Any] = {"messages": [{"role": "user", "content": text}]}
        return self._consume(graph, payload, handle)

    async def _branch_messages(
        self, thread_id: str, principal: Principal | None
    ) -> list[Any]:
        """读当前分支的消息列表，并顺带完成权限与会话校验。

        WHY 读「当前分支」而不是会话最新状态：分叉之后两者不再是同一件事。用户在旧
        分支上点重新生成，理应接着**那条**分支的上下文，而不是最新那条的。

        Raises:
            PermissionDeniedError: 缺少 thread:create 权限。
            NotFoundError: 会话或分支不存在。
            OwnershipError: 无权访问该会话。
            RuntimeError: 读取失败。
        """
        self._ensure_permission(principal, "thread:create")
        await self._ensure_ownership(thread_id, principal)

        graph = self._graph_factory.get()
        branch = await self._thread_store.current_branch(thread_id)
        checkpoint = await self._branch_head(graph, thread_id, branch)

        try:
            state = await graph.aget_state(
                build_runnable_config(self._config, thread_id, checkpoint)
            )
        except Exception as exc:
            logger.exception("读取分支历史失败：thread=%s branch=%s", thread_id, branch)
            raise RuntimeError(f"读取分支历史失败：thread={thread_id}") from exc

        return list(getattr(state, "values", {}).get("messages") or [])

    async def _branch_head(self, graph: Any, thread_id: str, branch: str) -> str | None:
        """返回某分支的头部检查点 id；``None`` 表示「跟随会话当前的头」。

        WHY 当前分支不查表：它的头随每次运行前移，存下来的值必然过期。表里存的是
        「离开该分支时冻结的那个头」——那才是切回来时要用的东西。

        Raises:
            NotFoundError: 该分支既不是当前分支、也没登记过。
        """
        current = await self._thread_store.current_branch(thread_id)
        if branch == current:
            return None

        record = await self._thread_store.get_branch(thread_id, branch)
        head = (record or {}).get("head_checkpoint") or ""
        if not head:
            raise NotFoundError("分支", branch)
        return head

    async def _live_head(self, graph: Any, thread_id: str) -> str:
        """读会话当前的头部检查点 id。

        Raises:
            ValueError: 会话还没有任何检查点（空会话无从分叉）。
        """
        state = await graph.aget_state(build_runnable_config(self._config, thread_id))
        checkpoint = (
            (getattr(state, "config", None) or {}).get("configurable", {}).get("checkpoint_id", "")
        )
        if not checkpoint:
            raise ValueError("该会话还没有可用的检查点，无法分叉")
        return checkpoint

    async def _find_fork_checkpoint(
        self,
        graph: Any,
        thread_id: str,
        messages: list[Any],
        target_index: int,
    ) -> str:
        """找出「第 ``target_index`` 条消息出现之前」那个检查点。

        WHY 必须逐条比对消息链：``aget_state_history`` 给出的是**整个会话**的检查点，
        其中还包含其它分支的；只按消息条数挑会挑到别的分支上——后果是分叉后的上下文
        变成用户没写过的一段历史，而且没有任何报错，只能靠人眼发现。

        Raises:
            ValueError: 找不到匹配的历史（例如检查点已被清理）。
        """
        wanted = messages[:target_index]
        config = build_runnable_config(self._config, thread_id)

        async for snapshot in graph.aget_state_history(config):
            values = list(getattr(snapshot, "values", {}).get("messages") or [])
            if _same_message_chain(values, wanted):
                return snapshot.config["configurable"]["checkpoint_id"]

        raise ValueError("找不到该轮次对应的分叉点，历史可能已被清理")

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
            PermissionDeniedError: 缺少 hitl:approve 权限。
            NotFoundError: 会话不存在。
            OwnershipError: 无权访问该会话。
            InterruptExpiredError: 该会话的审批挂起已超过 TTL，本次恢复被拒绝。
            RuntimeError: 模型初始化或装配失败。
        """
        # WHY 审批需要独立权限：这一调用会让此前被拦下的高危工具真正执行，
        # 风险量级高于「发起对话」，不能复用 thread:create。
        self._ensure_permission(principal, "hitl:approve")

        normalized = normalize_thread_id(thread_id)
        # WHY 在这里就完成审批载荷校验：非法载荷必须在事件流开始之前失败，
        # 否则只能表现为连接中断，前端拿不到任何可读的失败原因。
        command = build_resume_command(decision_payload)
        graph = self._graph_factory.get(model_name)

        await self._ensure_ownership(normalized, principal)

        # WHY 在清除登记之前先判过期：过期标记正是「这次挂起已作废」的唯一
        # 凭据，若先把登记清掉，判定就永远为假，TTL 形同虚设，而用户却拿到了
        # 一次「看似成功」的恢复。
        if self.is_hitl_expired(normalized):
            ttl = self._config.hitl_pending_ttl_seconds
            logger.warning("会话 %s 的审批已超期（TTL=%s 秒），拒绝恢复", normalized, ttl)
            raise InterruptExpiredError(normalized, ttl)

        # WHY 恢复即意味着那一次审批已被应答：挂起登记必须在此刻清除，
        # 否则「待审批数」只会单调递增，失去作为运行治理指标的意义。
        self.clear_hitl_pending(normalized)

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

        handle = self._acquire_run_slot(
            normalized,
            model_name=model_name,
            owner_id=self._owner_id(principal),
            actor_id=actor_id,
        )
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
        # WHY 同步终止子进程树（T7-f）：置位只让事件循环侧停止产出事件，
        # 真正跑着的 shell 命令在工作线程里，它只认自己的超时——不显式终止，
        # 「已停止」就只是接口层确认，UI 复位后命令最长还能活一个 SANDBOX_TIMEOUT。
        aborted = abort_scope(normalized)
        logger.info(
            "会话 %s 收到停止请求：actor=%s 已运行 %.1f 秒，终止在跑命令 %d 个",
            normalized,
            actor_id,
            handle.elapsed_seconds,
            aborted,
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

    async def enforce_governance(self) -> GovernanceReport:
        """巡检一次运行治理：强制取消超时运行、作废超期未决策的审批挂起。

        由后台协程按 ``run_governance_interval_seconds`` 调用；也允许运维在
        测试中手动触发一次。

        WHY 把两件事放在一次巡检里：它们共享同一份「运行即时状态」的快照，
        也共享同一条审计口径（动作主体都是系统）；分成两个协程就要两把锁的
        快照语义，反而更容易出现「刚判定超时、同一轮又判它挂起过期」的
        自相矛盾记录。

        Returns:
            本次巡检的结果（超时数、过期数、巡检到的运行/挂起数）。

        Raises:
            RuntimeError: 阈值配置非法（由配置校验兜底，理论不可达）。
        """
        limit = self._config.run_max_seconds
        ttl = self._config.hitl_pending_ttl_seconds
        if not isinstance(limit, int) or limit < 0:
            raise RuntimeError("run_max_seconds 配置非法")
        if not isinstance(ttl, int) or ttl < 0:
            raise RuntimeError("hitl_pending_ttl_seconds 配置非法")

        now = time.monotonic()
        running_snapshot = self.run_handles()
        pending_snapshot = self.pending_hitl_thread_ids()
        report = GovernanceReport(
            checked_runs=len(running_snapshot),
            checked_hitl=len(pending_snapshot),
        )

        report.timed_out_runs = await self._enforce_run_timeouts(running_snapshot, limit, now)
        report.expired_hitl = await self._expire_stale_hitl(pending_snapshot, ttl)

        if report.timed_out_runs or report.expired_hitl:
            logger.info(
                "运行治理巡检：超时取消 %d 个运行，作废 %d 个超期审批",
                report.timed_out_runs,
                report.expired_hitl,
            )
        return report

    async def _enforce_run_timeouts(
        self,
        running_snapshot: dict[str, RunHandle],
        limit: int,
        now: float,
    ) -> int:
        """强制取消超过 ``run_max_seconds`` 的运行，返回被取消的数量。"""
        if limit <= 0:
            return 0

        cancelled = 0
        for thread_id, handle in running_snapshot.items():
            if handle.stop_requested or (now - handle.started_at) < limit:
                continue
            # WHY 二次确认句柄仍在册：快照到此刻之间该运行可能已自然结束，
            # 若直接置位，就会对一个已废弃的句柄记一次超时审计。
            if self.run_handle(thread_id) is not handle:
                continue

            elapsed = handle.elapsed_seconds
            handle.request_stop(STOP_REASON_TIMEOUT)
            # WHY 超时同样要终止子进程树：超时往往正是命令卡住造成的，
            # 只取消 future 会让那棵进程树继续活到它自己的超时。
            aborted = abort_scope(thread_id)
            with self._running_guard:
                self._timed_out_runs += 1
            cancelled += 1
            logger.warning(
                "会话 %s 运行超过 %d 秒（实际 %.1f 秒），已强制取消，终止在跑命令 %d 个",
                thread_id,
                limit,
                elapsed,
                aborted,
            )
            await self._audit(
                event_type="run_timeout",
                actor_id=SYSTEM_ACTOR,
                target_id=thread_id,
                action="timeout",
                outcome="success",
                details={
                    "elapsed_seconds": round(elapsed, 3),
                    "max_seconds": limit,
                },
            )
        return cancelled

    async def _expire_stale_hitl(
        self,
        pending_snapshot: tuple[str, ...],
        ttl: int,
    ) -> int:
        """作废挂起超过 ``hitl_pending_ttl_seconds`` 的审批，返回作废数量。

        WHY 用「当前挂起时长」而不是传入的统一 ``now`` 再减：判定与作废之间
        隔着一次 await（写审计），期间用户完全可能应答；以服务内的实时时长
        为准，可以让刚刚被应答的会话不会被误判。
        """
        if ttl <= 0:
            return 0

        expired = 0
        for thread_id in pending_snapshot:
            age = self.hitl_pending_age(thread_id)
            if age is None or age < ttl:
                continue
            # WHY 以 expire_hitl_pending 的返回值为准：用户可能刚好在这一刻
            # 应答（resume 会先清登记），此时本次巡检应当让位，而不是把一次
            # 已经生效的审批再标记成过期。
            if not self.expire_hitl_pending(thread_id):
                continue
            expired += 1
            await self._audit(
                event_type="hitl_expired",
                actor_id=SYSTEM_ACTOR,
                target_id=thread_id,
                action="expire",
                outcome="success",
                details={
                    "pending_seconds": round(age, 3),
                    "ttl_seconds": ttl,
                },
            )
        return expired

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
            # WHY 把本次运行绑定到执行登记处的作用域：同步工具节点由
            # LangGraph 用 ``run_in_executor(copy_context)`` 调度到工作线程，
            # contextvar 是唯一能在不改上游协议的前提下把「会话身份」带进
            # 命令执行器的通道；有了它，stop 才能顺着会话找到那棵进程树。
            with bound_scope(thread_id):
                async for event in self._iterate(graph, payload, handle):
                    if event.event is AgentEventType.USAGE:
                        # WHY 在这里落库而不是在 _iterate 里：写库是带副作用的动作，
                        # 而 _iterate 只负责翻译流。让「产出事件」与「持久化」分层，
                        # 事件流本身在无存储的环境下（CLI、测试）依然完整。
                        await self._record_usage(handle, event.payload)
                    yield event

                # WHY 只在正常收尾（含被停止）时落工具审计：客户端断开触发的
                # ``CancelledError`` 会直接跳出本块，此时运行尚未结束、工具可能
                # 仍在执行，写下的会是「进行中」的假记录；这类运行以 TCP 断开
                # 结束，另有连接层日志可查。
                await self._audit_tool_calls(handle)
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
            # WHY 用句柄上记录的原因而不是写死 "stopped"：超时由后台协程置位，
            # 用户停止由 stop() 置位，两者都必须让前端看到「不是正常收尾」，
            # 但提示语不同（「已停止」vs「已超时终止」）。取不到原因时
            # 退化为 "stopped"，避免出现没有 reason 的半截语义。
            done_payload["reason"] = handle.stop_reason or STOP_REASON_STOPPED
        logger.info("会话 %s 本轮结束", thread_id)
        yield AgentEvent(AgentEventType.DONE, done_payload)

    def _persist_outputs(self, batch: list[tuple[Path, str]]) -> None:
        """在工作线程里写留存文件，并按上限清理该会话的旧留存。

        WHY 单独成方法而不是写在闭包里：它要在 ``to_thread`` 里跑，写成独立方法
        既便于测试直接调用，也让「阻塞 IO 只发生在这里」这件事在结构上看得见。
        """
        for path, text in batch:
            write_tool_output(
                path, text, max_chars=self._config.tool_output_max_chars
            )
        removed = prune_tool_outputs(
            batch[0][0].parent, keep=self._config.tool_output_retention_per_thread
        )
        if removed:
            logger.info(
                "工具输出留存清理：删除 %d 个旧文件（目录=%s）",
                removed,
                batch[0][0].parent,
            )

    async def _iterate(
        self,
        graph: Any,
        payload: Any,
        handle: RunHandle,
    ) -> AsyncIterator[AgentEvent]:
        """逐条翻译流增量，并负责运行期错误收敛。"""
        pending_outputs: list[tuple[Path, str]] = []
        output_sequence = itertools.count(1)

        def capture_full_output(tool_name: str, full_text: str) -> str:
            """算出留存引用并暂存正文，返回供事件使用的虚拟路径。

            WHY 只暂存不写盘：本回调由翻译器**同步**调用，而写文件是阻塞 IO——
            在事件循环里直接写会让流式输出卡顿。落盘交给下面的 ``flush_outputs``。
            """
            path = tool_output_path(
                self._config.workspace, handle.thread_id, next(output_sequence), tool_name
            )
            pending_outputs.append((path, full_text))
            return to_virtual_path(self._config.workspace, path)

        async def flush_outputs() -> None:
            """把暂存的留存写盘。

            WHY 每批事件后立刻写而不是攒到整轮结束：被取消或被停止的运行走不到
            收尾分支，攒到最后的输出会整批丢失——而「用户中途停掉」恰恰是最想
            回看完整结果的场景。
            """
            if not pending_outputs:
                return
            batch = list(pending_outputs)
            pending_outputs.clear()
            try:
                await asyncio.to_thread(self._persist_outputs, batch)
            except Exception:
                # 留存是旁路能力：写不进去不该让一轮已经跑完的对话崩掉，
                # 但必须留日志，否则「为什么没有留存」只能靠猜。
                logger.exception("工具输出留存失败，已跳过（不影响本轮对话）")

        translator = LangGraphEventTranslator(
            tool_result_preview_limit=self._config.tool_result_preview_chars,
            full_output_capture=capture_full_output,
        )

        try:
            async for mode, chunk in self._stream_graph(graph, payload, handle):
                for event in translator.feed(mode, chunk):
                    self._track_event(event, handle)
                    yield event
                await flush_outputs()

            # 流结束后冲出最后一批未发送的工具调用
            for event in translator.flush():
                # WHY 这里也要过一遍 _track_event：这些 TOOL_CALL 是真实发生
                # 在流末（模型节点结束）的调用，审计与挂起登记的统计口径若
                # 漏掉它们，就会出现「工具调用了但审计里没有」的缺口。
                self._track_event(event, handle)
                yield event
            await flush_outputs()

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

        # WHY 无论正常结束、被用户停止还是运行出错都上报用量：三种情况下
        # token 都已经被真实消耗了。停止与出错时拿到的只是「部分用量」，
        # 但部分用量也比没有任何数字更能回答「这次花了多少」。
        if translator.has_usage:
            usage = translator.usage
            logger.info(
                "会话 %s 本轮用量：prompt=%d completion=%d",
                handle.thread_id,
                usage.prompt_tokens,
                usage.completion_tokens,
            )
            yield AgentEvent(AgentEventType.USAGE, usage.as_payload())
        else:
            # WHY 缺失必须留日志而不是静默跳过：长期为 0 意味着 provider 换了
            # 字段口径而本模块的映射没跟上，静默会让成本统计悄悄失真。
            logger.warning(
                "会话 %s 本轮未取到 token 用量（provider 未提供或字段口径变更），按 0 记录",
                handle.thread_id,
            )
            yield AgentEvent(AgentEventType.USAGE, TokenUsage(0, 0).as_payload())

    def _track_event(self, event: AgentEvent, handle: RunHandle) -> None:
        """按事件类型维护运行治理所需的派生状态。

        WHY 单独成方法：``_iterate`` 是事件流的热路径，把状态维护内联进去
        会让「翻译 → 转发」的主线被治理逻辑淹没；独立后新增一种需要跟踪的
        事件类型只改这一处。
        """
        if event.event is AgentEventType.INTERRUPT:
            # 中断意味着本轮运行已暂停等待人工决策，此刻登记挂起，
            # 由 resume 或下一轮用户输入清除。
            self.mark_hitl_pending(handle.thread_id)
        elif event.event is AgentEventType.TOOL_CALL:
            handle.tool_calls.append(
                ToolCallRecord(
                    name=str(event.payload.get("name") or "unknown"),
                    started_at=time.monotonic(),
                    args_preview=_preview_args(event.payload.get("args")),
                )
            )
        elif event.event is AgentEventType.TOOL_RESULT:
            self._close_tool_call(handle, event.payload)

    @staticmethod
    def _close_tool_call(handle: RunHandle, payload: dict[str, Any]) -> None:
        """把工具结果配回到最近一条未结束的同名调用上。

        WHY 从后往前找同名记录：并行工具调用时结果与调用的顺序并不保证
        一致，按名字 + 未结束两个条件匹配是唯一不依赖顺序的做法；找不到
        就丢弃这条结果的计时，而不是错误地记到别的工具上。
        """
        name = str(payload.get("name") or "")
        for record in reversed(handle.tool_calls):
            if record.status or record.elapsed_ms is not None:
                continue
            if name and record.name != name:
                continue
            record.status = str(payload.get("status") or "")
            record.elapsed_ms = int((time.monotonic() - record.started_at) * 1000)
            return

    async def _audit_tool_calls(self, handle: RunHandle) -> None:
        """落库本轮的工具调用审计。

        WHY 在流结束后统一写而不是每次调用都写：``_track_event`` 处在事件
        转发的热路径上，逐条 await 写库会让每一次工具调用都多一次 IO 往返；
        先收集再批量落库，事件流的时延不受审计影响。

        WHY 只审计扩展工具（默认）：内置文件操作频次高、风险已被 HITL 审批
        覆盖，全部落库会让审计表随对话量线性膨胀；需要排查内置工具时可用
        ``tool_audit_builtin`` 打开。
        """
        if self._audit_store is None or not handle.tool_calls:
            return

        actor_id = handle.actor_id or "anonymous"
        for record in handle.tool_calls:
            source = self._tool_catalog.source_of(record.name) if self._tool_catalog else "unknown"
            if source == "builtin" and not self._config.tool_audit_builtin:
                continue
            outcome = "success"
            if not record.status:
                outcome = "interrupted"
            elif record.status.lower() == "error":
                outcome = "error"
            await self._audit(
                event_type="tool_call",
                actor_id=actor_id,
                target_id=handle.thread_id,
                action=record.name,
                outcome=outcome,
                details={
                    "tool": record.name,
                    "source": source,
                    "server": self._tool_catalog.server_of(record.name) if self._tool_catalog else None,
                    "status": record.status or "interrupted",
                    "elapsed_ms": record.elapsed_ms,
                    "args_preview": record.args_preview,
                },
            )
        handle.tool_calls.clear()

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
            config=build_runnable_config(
                self._config, handle.thread_id, handle.fork_checkpoint or None
            ),
            stream_mode=_STREAM_MODES,
            # WHY 必须显式传 context：长期记忆的命名空间在图内按主体计算，
            # 缺了它记忆会落进匿名池——多用户部署下等于跨用户串味。
            context=AgentRunContext(user_id=handle.memory_owner),
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

    def _acquire_run_slot(
        self,
        thread_id: str,
        *,
        model_name: str | None = None,
        owner_id: str = "",
        actor_id: str = "",
        fork_checkpoint: str = "",
    ) -> RunHandle:
        """占用该会话的运行槽位并登记运行句柄。

        WHY 必须互斥：同一会话并发发起两轮会让图状态产生竞争——两轮各自读写
        同一 thread 的检查点，后写的一方会覆盖先写一方的中间结果，表现为消息
        丢失或工具结果错配。

        WHY 把模型别名与所有者一起登记进句柄：用量在流结束时才落库，那一刻
        已经拿不到本轮的参数；挂在句柄上才能「谁的模型、谁的用量」对齐。

        Args:
            thread_id: 已规范化的会话 ID。
            model_name: 本轮使用的模型别名；``None`` 表示默认模型。
            owner_id: 会话所有者；认证关闭时为空串。
            actor_id: 发起本轮运行的主体标识，用于工具审计归因。

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
                model_name=model_name,
                owner_id=owner_id,
                actor_id=actor_id,
                fork_checkpoint=fork_checkpoint,
            )
            self._running[thread_id] = handle
            # 累计运行数在此累加：这里是「一轮运行真正开始」的唯一入口，
            # 放在 stream / resume 里会漏掉其中一条路径。
            self._started_runs += 1
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

    def run_handles(self) -> dict[str, RunHandle]:
        """当前全部运行句柄的快照（会话 ID → 句柄）。

        WHY 需要整表快照而不是逐个查 ``run_handle``：运行治理要在同一时刻
        判断「哪些运行超时」，逐个查询会让每个判断落在不同时刻，从而把扫描
        期间才启动的运行也算进本轮结论里。
        """
        with self._running_guard:
            return dict(self._running)

    def running_thread_ids(self) -> tuple[str, ...]:
        """当前运行中的会话 ID 快照（供指标暴露）。"""
        with self._running_guard:
            return tuple(self._running)

    @property
    def started_runs(self) -> int:
        """进程启动以来累计发起的运行次数。

        WHY 做成指标：单看「运行中」只能知道当下忙不忙，累计值才能回答
        「这台实例跑过多少轮」，是容量规划与异常检测的最小数据集。
        """
        with self._running_guard:
            return self._started_runs

    def mark_hitl_pending(self, thread_id: str) -> None:
        """登记该会话有一个等待人工审批的中断，并记录挂起起始时刻。

        WHY 由服务持有而不是让指标端点去遍历图状态：遍历需要对每个会话
        调一次 ``aget_state``，成本随会话数线性增长；而中断事件在事件流里
        已经出现过一次，登记是零成本的。

        WHY 重复登记不刷新起始时刻：同一轮运行里中断事件可能出现多次
        （多个待审批工具），若每次都重置，TTL 就永远走不完；以首次登记
        为准才能让「挂起太久」这个判断成立。

        Args:
            thread_id: 已规范化的会话 ID。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")
        with self._running_guard:
            # 新一轮中断作废上一次的过期标记：既然又等上了，说明用户确实
            # 在跟这个会话交互，此前的过期结论不再适用。
            self._hitl_expired.pop(thread_id, None)
            if thread_id not in self._hitl_pending:
                self._hitl_pending[thread_id] = time.monotonic()

    def clear_hitl_pending(self, thread_id: str) -> None:
        """清除该会话的待审批登记与过期标记；本就没有挂起时是 no-op。

        Args:
            thread_id: 会话 ID。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")
        with self._running_guard:
            self._hitl_pending.pop(thread_id, None)
            self._hitl_expired.pop(thread_id, None)

    def expire_hitl_pending(self, thread_id: str) -> bool:
        """把该会话的挂起审批标记为过期并释放占位。

        Args:
            thread_id: 会话 ID。

        Returns:
            是否真的作废了一次挂起；``False`` 表示该会话此刻没有挂起
            （已被用户应答或已被并发清理过），调用方据此跳过后续处理。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")
        with self._running_guard:
            started_at = self._hitl_pending.pop(thread_id, None)
            if started_at is None:
                return False
            self._hitl_expired[thread_id] = time.monotonic()
            self._expired_hitl += 1
        logger.info(
            "会话 %s 的审批挂起已超期作废：等待 %.1f 秒",
            thread_id,
            time.monotonic() - started_at,
        )
        return True

    def is_hitl_expired(self, thread_id: str) -> bool:
        """该会话是否有一个已作废（超期）的审批挂起。"""
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")
        with self._running_guard:
            return thread_id in self._hitl_expired

    def hitl_pending_age(self, thread_id: str) -> float | None:
        """该会话的审批已挂起秒数；未挂起时为 ``None``。"""
        with self._running_guard:
            started_at = self._hitl_pending.get(thread_id)
        return None if started_at is None else time.monotonic() - started_at

    def pending_hitl_thread_ids(self) -> tuple[str, ...]:
        """当前等待人工审批的会话 ID 快照。

        WHY 需要这份登记：HITL 挂起的运行既不占运行槽位（图已经暂停），
        也不是错误，只有单独记录才能被指标与后续的挂起 TTL 治理看到。
        """
        with self._running_guard:
            return tuple(self._hitl_pending)

    @property
    def timed_out_runs(self) -> int:
        """进程启动以来被运行超时强制取消的运行次数。"""
        with self._running_guard:
            return self._timed_out_runs

    @property
    def expired_hitl(self) -> int:
        """进程启动以来因超期未决策而作废的审批挂起次数。"""
        with self._running_guard:
            return self._expired_hitl

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

    async def _record_usage(self, handle: RunHandle, payload: dict[str, Any]) -> None:
        """把本轮用量写入用量表。

        WHY 吞掉异常：用量是旁路数据，写库失败不应让一次成功的对话变成
        错误响应，也不该影响已经产出的事件流；失败会留下完整日志供告警，
        代价只是这一轮的用量缺失。

        Args:
            handle: 本次运行的句柄，提供会话、模型别名与所有者。
            payload: USAGE 事件的载荷（prompt / completion / total）。
        """
        if self._usage_store is None:
            return

        prompt = payload.get("prompt_tokens") or 0
        completion = payload.get("completion_tokens") or 0
        try:
            await self._usage_store.record(
                thread_id=handle.thread_id,
                model=handle.model_name or self._config.default_model,
                prompt_tokens=int(prompt),
                completion_tokens=int(completion),
                owner_id=handle.owner_id,
            )
        except Exception:
            logger.exception("用量记录失败：thread=%s", handle.thread_id)

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
