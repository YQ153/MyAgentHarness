"""运行服务：发起对话、人工审批后恢复执行。

职责边界：只负责「把一次运行推进到底并产出事件」，不负责会话清单与历史
（见 ``application.thread_service.ThreadService``）。

本模块原先是一份 1800 余行的单文件实现，现已按职责拆成四块：
- ``application.run_registry``    运行槽位、计数与审批挂起状态的唯一所有者；
- ``application.run_governance``  超时取消与审批挂起的定时收口；
- ``application.run_branch``      从历史检查点分叉再跑一轮（编辑 / 重新生成）；
- 本模块：入口校验、事件流的翻译与收尾、审计与用量落库。

WHY 本类保留「一行方法体 + 委托」的形态，而不是让调用方直接持有协作者：
``RunService`` 是接口层与装配层唯一认识的运行入口，它的方法名、签名与返回值是
既有契约（``tests/application/test_run_*.py`` 八个文件全部按它编写）。结构调整
不该顺带改契约，否则一次「让文件变小」的改动会变成一次全仓改动。内部步骤名
（如 ``_acquire_run_slot``）同样保留——它们同时是被测试直接驱动的入口。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.run_context import AgentRunContext
from application.audit_context import LOCAL_ACTOR_ID, audit_client_info, audit_trace_id
from application.dto import GovernanceReport
from application.errors import (
    InterruptExpiredError,
    NotFoundError,
    ThreadBusyError,
)
from application.event_translator import LangGraphEventTranslator
from application.events import AgentEvent, AgentEventType
from application.interrupt_codec import build_resume_command
from application.message_utils import (
    last_user_index,
    message_text,
    role_of,
    user_turn_number,
)
from application.ports import AuditLog, ThreadMetadataStore, UsageLedger
from application.run_branch import RunBranchService
from application.run_governance import RunGovernor
# RunHandle / ToolCallRecord / STOP_REASON_* 在此一并再导出：句柄是 ``run_handle``
# 的返回类型，停止原因是 DONE 事件 payload 的取值——都是本类对外契约的一部分。
# 调用方不应为了拿一个类型或常量去耦合实现模块。
from application.run_registry import (
    STOP_REASON_STOPPED,
    STOP_REASON_TIMEOUT,
    RunHandle,
    RunRegistry,
    ToolCallRecord,
)
from application.runnable import build_runnable_config
from application.usage import TokenUsage
from application.session_registry import SessionRegistry
from runtime.execution_registry import abort_scope, bound_scope
from runtime.tool_outputs import (
    prune_tool_outputs,
    tool_output_path,
    tool_output_virtual_path,
    write_tool_output,
)
from text_utils import build_title
from thread_utils import normalize_thread_id


if TYPE_CHECKING:
    from agent.graph import AgentFactory
    from application.tool_catalog import ToolCatalog
    from config import AppConfig

# WHY 显式声明对外名字：其中四个是从 ``application.run_registry`` 再导出的契约名
# （见上方 import 处的说明）。写成 ``__all__`` 而不是依赖隐式的再导出，是为了让
# 「谁在用哪个名字」能被静态检查看见，而不是一个看不见的副作用。
__all__ = [
    "STOP_REASON_STOPPED",
    "STOP_REASON_TIMEOUT",
    "RunHandle",
    "RunService",
    "ToolCallRecord",
]

logger = logging.getLogger(__name__)

_STREAM_MODES = ["messages", "updates"]
"""本轮运行订阅的两种 ``stream_mode``，缺一不可。

WHY 两种都要：``messages`` 出文本与工具调用的分片，``updates`` 出中断、节点进度与
待办快照；只订一种会让另一类事件彻底不再浮现，而事件流的消费方无从察觉。
"""

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
        thread_store: ThreadMetadataStore,
        graph_factory: AgentFactory,
        workspaces: SessionRegistry,
        audit_store: AuditLog | None = None,
        usage_store: UsageLedger | None = None,
        tool_catalog: ToolCatalog | None = None,
    ) -> None:
        """构造运行服务。

        Args:
            config: 应用配置。
            thread_store: 会话元数据存储，用于登记轮次与刷新活动时间。
            graph_factory: 图工厂，提供已装配的 LangGraph 图。
            workspaces: 会话级工作区的解析与装配入口；**必填**——本轮的文件根、
                技能来源与工具输出留存都按它解析出的工作区换算。
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
        if workspaces is None:
            raise ValueError("workspaces 不能为 None：本轮跑在哪个工作区由它解析")

        self._config = config
        self._thread_store = thread_store
        self._graph_factory = graph_factory
        self._workspaces = workspaces
        self._audit_store = audit_store
        self._usage_store = usage_store
        self._tool_catalog = tool_catalog

        # WHY 三个协作者在构造期一次装配、而不是由装配层分别注入：它们必须与本次
        # 构造共享同一份配置与**同一张**登记表（入口准入、治理巡检与指标端点读写的
        # 是同一份运行即时状态）。从外面拼装只会多出一条「谁先把谁造出来」的顺序
        # 约束，而顺序错了的后果是指标静默失真，不是报错。
        self._registry = RunRegistry(config)
        self._governor = RunGovernor(config, registry=self._registry, audit=self._audit)
        self._branches = RunBranchService(
            config,
            thread_store=thread_store,
            graph_factory=graph_factory,
            registry=self._registry,
            workspaces=workspaces,
            audit=self._audit,
        )

        logger.info(
            "RunService 就绪：mode=%s recursion_limit=%s",
            config.execution_mode.value,
            config.recursion_limit,
        )

    async def _load_record(
        self, thread_id: str, *, allow_claim: bool = False
    ) -> dict[str, Any]:
        """读取会话元数据，并按需要放宽「会话必须已登记」这一条。

        Args:
            thread_id: 已规范化的会话 ID。
            allow_claim: 允许会话尚未登记——``stream`` 的首条消息就发生在登记之前，
                此时返回空字典；``False`` 时会话不存在即报错。

        Returns:
            会话元数据；``allow_claim`` 且会话未登记时返回空字典。

        Raises:
            NotFoundError: 会话不存在且 ``allow_claim=False``。
        """
        record = await self._thread_store.get(thread_id)
        if record is None:
            if allow_claim:
                return {}
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
                # WHY 与 IP/UA 同一处读取：三者都是「这次请求的来路」，分开读会让
                # 后续新增的读取点只记得其中一个，表现为部分审计记录没有链路标识。
                trace_id=audit_trace_id(),
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
        user_input: str | list[dict[str, Any]],
        *,
        model_name: str | None = None,
        workspace: str | None = None,
        preset: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """发起一轮对话。

        WHY 本方法是 ``async def`` 且**不含** ``yield``：异步生成器要等到首次
        ``__anext__()`` 才执行函数体，参数校验与模型初始化若写在里面，就要等到
        响应已经开始之后才抛错，客户端只能看到「连接被中断」。写成普通协程可以
        让这些前置失败在 ``await service.stream(...)`` 时就抛出，调用方得以返回
        正常的 HTTP 状态码。

        Args:
            thread_id: 会话 ID。
            user_input: 用户本轮输入。纯文本时为字符串；带附件时是多模态内容块列表
                （由 ``AttachmentService.build_user_content`` 构造）。两种形态都必须
                含非空文本——标题、日志与「轮次是否成立」都依赖它。
            model_name: 模型别名；``None`` 表示使用默认模型。
            workspace: 会话的工作空间路径；``None`` 表示**不绑定工作空间**——这条会话
                将使用应用为它自动创建的专属目录。取值**只在首条消息上生效**：一旦会话
                产生过交互，它的文件根就锁定了，再给出不同的取值会被拒绝
                （见 ``SessionRootLockedError``）。
            preset: 会话的**场景预设 ID**；``None`` / 空串表示不限定（接受全部技能）。
                与 ``workspace`` 同样**只在首条消息上生效**，且同一个工作空间只允许一个
                场景（视图按根物化，两个场景会互相覆盖，见 ``SessionPresetLockedError``）。

        Returns:
            产出统一事件的异步迭代器。

        Raises:
            ValueError: ``thread_id``、``user_input`` 非法，或 ``workspace`` 指向的
                目录不存在。
            KeyError: 模型别名未注册。
            NotFoundError: 会话不存在。
            SessionRootLockedError: ``workspace`` 与该会话已锁定的文件根不一致。
            SessionPresetLockedError: ``preset`` 与该会话已锁定的场景不一致。
            RuntimeError: 模型初始化或装配失败。
        """
        # WHY 限流排在任何「查这个会话存不存在」的动作之前：否则被限流的一方能从
        # 404 与 429 的差别里推断出会话是否存在。
        self._check_run_limits()

        normalized = normalize_thread_id(thread_id)
        text, content = _split_user_input(user_input)

        # 首条消息可能还未登记元数据，因此允许会话此刻尚不存在。
        record = await self._load_record(normalized, allow_claim=True)

        # WHY 文件根必须在这里（取图之前）定下来：图的文件根、技能来源与容器挂载根
        # 都在装配那一刻烧死，而根是会话级取值——顺序反了就会拿到「上一个根」的图，
        # 运行过程毫无异常，文件却写进了另一个项目。
        scope = await self._workspaces.resolve(
            requested=workspace, thread_id=normalized, record=record, preset=preset
        )

        # WHY 在取图之前把该根的技能视图对齐到 ``scope``：图里烧进去的技能来源是挂载出来的
        # ``/.skills-active``，而这份视图按**工作空间 + 场景**物化、由 ``services()`` 重建。
        # 场景是在首条消息这一刻才确定的，而这个根可能已经被「场景还没定下来」的草稿态请求
        # （面板、附件解析）先装配过一次；不在取图前对齐，图就会照着那份旧视图运行——预设
        # 里的技能一个都进不了上下文，而日志上一切正常（视图重建的 INFO 只在装配时打印）。
        # 重复调用是廉价的：场景相同直接命中缓存，不重建。
        await self._workspaces.services(scope)

        # WHY 新一轮用户输入会作废此前悬着的审批请求：用户既已改口，那个
        # 审批卡就不再代表当前意图；留着它只会让「待审批数」无限增长。
        self.clear_hitl_pending(normalized)

        # WHY 在进入图之前取图：这一步会解析模型别名并真正初始化模型，
        # 把配置与密钥错误暴露在事件流开始之前。
        graph = self._graph_factory.get(model_name, scope=scope)

        actor_id = LOCAL_ACTOR_ID
        logger.info(
            "会话 %s 发起运行（%d 字符）actor=%s workspace=%s",
            normalized,
            len(text),
            actor_id,
            scope.root,
        )

        # WHY 在进入图之前登记：这一刻才是会话真正诞生的时刻。放在轮次结束后
        # 登记，会让「模型初始化失败」这类早退场景下的会话凭空消失，而用户
        # 明明已经表达过意图。标题也取自这次输入——唯一「用户明确表达意图」
        # 的文本，不需要额外调用模型。
        #
        # WHY 一并写入文件根：锁定必须发生在「这条会话开始跑」的那一刻，晚一步就会出现
        # 「会话已经跑过、根还没记下」的空窗，而那个空窗里的重试会按另一条规则重算一次
        # ——落进另一个目录。
        # WHY 传 workspace_bound：它区分「用户选的工作空间」与「应用给的专属目录」，
        # 界面靠它决定文案；这个事实只在锁定那一刻知道，事后再也推断不出来。
        await self._record_turn(
            normalized,
            title_hint=text,
            turn_delta=1,
            workspace=str(scope.root),
            workspace_bound=bool(workspace and str(workspace).strip()),
            # WHY 写 ``scope.preset`` 而不是请求里的 preset：解析层已经把「库里的值」与
            # 「本次请求的值」合并成了唯一结论（补选也在那里处理），这里再信一次请求参数
            # 就等于留了第二个真相。
            preset=scope.preset,
        )

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
            owner_id="",
            actor_id=actor_id,
            workspace=str(scope.root),
        )

        payload: dict[str, Any] = {"messages": [{"role": "user", "content": content}]}
        return self._consume(graph, payload, handle)

    # ------------------------------------------------------------------ 编辑与分叉

    async def regenerate(
        self,
        thread_id: str,
        *,
        model_name: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """重新生成最后一轮助手回复。

        WHY 与编辑共用同一条机制：两者都是「从某个历史检查点分叉，再用一段文本跑一次」，
        差别只在分叉点与新文本从哪来。分成两套实现会让并发、分支登记、用量归属
        各写一遍，而它们迟早分叉；合成一条路径则这些语义只存在一处。

        WHY 是分叉而不是原地重跑：旧回复所在的路径原样保留，用户不满意时还能切回去
        比对；这也让「成本不因重新生成而消失」自然成立——旧分支的用量记录一条都没删。

        Returns:
            产出统一事件的异步迭代器。

        Raises:
            ValueError: ``thread_id`` 非法，或该会话还没有可用的用户消息。
            NotFoundError: 会话或分支不存在。
            ThreadBusyError: 该会话已有运行中的轮次。
        """
        normalized = normalize_thread_id(thread_id)
        # WHY 限流排在 _branch_messages 之前：后者会读会话，而限流判定必须先于
        # 「会话是否存在」的结论给出，否则状态码本身成了探测手段。
        self._check_run_limits()

        messages = await self._branch_messages(normalized)

        last_user = last_user_index(messages)
        if last_user is None:
            raise ValueError("该会话还没有用户消息，无法重新生成")
        text = message_text(messages[last_user]).strip()
        if not text:
            raise ValueError("最后一条用户消息没有文本内容，无法重新生成")

        return await self._fork_and_run(
            normalized,
            text,
            messages=messages,
            target_index=last_user,
            origin="regenerate",
            label="重新生成",
            model_name=model_name,
        )

    async def edit(
        self,
        thread_id: str,
        message_index: int,
        content: str,
        *,
        model_name: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """改写指定下标的用户消息，并从该点分叉重跑。

        WHY 用下标而不是消息 id 定位：历史消息 DTO 一直不对外暴露 id，为编辑单独加一个
        字段会让前端必须先从两处对上号；下标在「某条分支的消息列表」内是确定的，而编辑
        本来就必须先看到那份列表。

        Raises:
            ValueError: ``thread_id`` 非法、下标越界、目标不是用户消息、或新文本为空。
            NotFoundError: 会话或分支不存在。
            ThreadBusyError: 该会话已有运行中的轮次。
        """
        normalized = normalize_thread_id(thread_id)
        if not isinstance(message_index, int) or isinstance(message_index, bool):
            raise ValueError(f"message_index 必须是整数，实际：{type(message_index).__name__}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content 必须是非空字符串")
        text = content.strip()

        # WHY 与 regenerate 同一顺序：先限流，最后才读会话。
        self._check_run_limits()

        messages = await self._branch_messages(normalized)
        if message_index >= len(messages):
            raise ValueError(f"message_index 越界（{message_index} >= {len(messages)}）")
        if role_of(messages[message_index]) != "user":
            raise ValueError("只能编辑用户消息")

        turn = user_turn_number(messages, message_index)
        return await self._fork_and_run(
            normalized,
            text,
            messages=messages,
            target_index=message_index,
            origin="edit",
            label=f"编辑第 {turn} 轮",
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
        model_name: str | None,
    ) -> AsyncIterator[AgentEvent]:
        """从「目标消息出现之前」的检查点分叉，并用 ``text`` 跑一轮。

        WHY 拼接动作只在这里发生：分叉的准备（定位检查点、登记分支、占槽位）与事件流
        的消费（翻译、落库、收尾）分属两个模块，但调用方只应看到一个「返回事件迭代器」
        的方法——拼接留在此处，两条路径（编辑 / 重新生成）都不必知道内部拆成了几块。

        Raises:
            ValueError: 找不到分叉点（历史已被清理）。
            ThreadBusyError: 该会话已有运行中的轮次。
        """
        plan = await self._branches.prepare_fork(
            thread_id,
            text,
            messages=messages,
            target_index=target_index,
            origin=origin,
            label=label,
            model_name=model_name,
            owner_id="",
            actor_id=LOCAL_ACTOR_ID,
        )
        return self._consume(plan.graph, plan.payload, plan.handle)

    async def _branch_messages(self, thread_id: str) -> list[Any]:
        """读当前分支的消息列表，并顺带完成会话校验。

        WHY 会话存在性校验留在这一层而不是下沉到分叉服务：它的结论必须先于
        「会话是否存在」给出（否则状态码本身成了探测手段），属于入口契约；分叉服务
        只管历史与分支记录。

        Raises:
            NotFoundError: 会话或分支不存在。
            RuntimeError: 读取失败。
        """
        await self._load_record(thread_id)

        return await self._branches.messages_of(thread_id)

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
            NotFoundError: 会话不存在。
            InterruptExpiredError: 该会话的审批挂起已超过 TTL，本次恢复被拒绝。
            RuntimeError: 模型初始化或装配失败。
        """
        self._check_run_limits()

        normalized = normalize_thread_id(thread_id)
        # WHY 在这里就完成审批载荷校验：非法载荷必须在事件流开始之前失败，
        # 否则只能表现为连接中断，前端拿不到任何可读的失败原因。
        command = build_resume_command(decision_payload)

        # WHY 会话记录在取图之前读：取图需要先知道本轮的工作区，而工作区要从会话
        # 记录里读。顺带的好处与 ``stream`` 一致——存在性问题比模型初始化便宜，
        # 让它先失败。
        record = await self._load_record(normalized)
        scope = await self._workspaces.resolve(thread_id=normalized, record=record)
        graph = self._graph_factory.get(model_name, scope=scope)

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

        actor_id = LOCAL_ACTOR_ID
        logger.info("会话 %s 恢复执行 actor=%s", normalized, actor_id)

        # WHY turn_delta=0：恢复是同一轮运行的延续，重复计数会让「对话轮数」
        # 与实际用户输入次数不符；但仍然要刷新活动时间。
        # WHY 同样带上工作区：未绑定过的历史会话会在这一刻被补记，此后它就有了明确
        # 归属，不必每轮都重新按默认值推断。
        await self._record_turn(
            normalized, title_hint=None, turn_delta=0, workspace=str(scope.root)
        )

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
            owner_id="",
            actor_id=actor_id,
            workspace=str(scope.root),
        )
        return self._consume(graph, command, handle)

    async def stop(
        self,
        thread_id: str,
    ) -> dict[str, Any]:
        """请求停止指定会话的当前运行。

        语义：只「触发」取消而不等待运行真正结束——已产出但尚未送达的事件
        会继续推送，运行最终以 DONE（payload 含 ``reason: "stopped"``）收尾。

        幂等：会话未在运行时返回 ``stopped=False``；对已请求过停止的会话
        重复调用返回 ``stopped=True``。二者都不是错误——「连点停止按钮」与
        「运行恰好在请求前一刻自然结束」不应让用户看到报错。

        Args:
            thread_id: 会话 ID。

        Returns:
            ``{"thread_id": str, "stopped": bool, "reason": str}``，其中
            ``reason`` 为 ``"requested"`` / ``"already_stopping"`` /
            ``"not_running"`` 三者之一。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话不存在。
        """
        normalized = normalize_thread_id(thread_id)
        await self._load_record(normalized)

        handle = self.run_handle(normalized)
        if handle is None:
            logger.info("会话 %s 收到停止请求：当前无运行", normalized)
            return {"thread_id": normalized, "stopped": False, "reason": "not_running"}

        if handle.stop_requested:
            logger.info("会话 %s 收到重复停止请求：忽略", normalized)
            return {"thread_id": normalized, "stopped": True, "reason": "already_stopping"}

        actor_id = LOCAL_ACTOR_ID
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

        WHY 本方法只留入口、不在正文里保留一段说明性的实现说明：巡检的两个动作
        （判定条件、收口动作、并发让位）属于「按时间自动发生」的问题域，它们的
        实现与这类问题的取舍一起落在 ``application.run_governance``。

        Returns:
            本次巡检的结果（超时数、过期数、巡检到的运行/挂起数）。

        Raises:
            RuntimeError: 阈值配置非法（由配置校验兜底，理论不可达）。
        """
        return await self._governor.enforce()

    async def _enforce_run_timeouts(
        self,
        running_snapshot: dict[str, RunHandle],
        limit: int,
        now: float,
    ) -> int:
        """强制取消超过 ``run_max_seconds`` 的运行，返回被取消的数量。

        WHY 保留这个私有入口而不是让调用方自己去拿治理器：它是
        ``enforce_governance`` 的一个步骤，测试也直接驱动它来复现「快照与置位
        之间运行已自然结束」的竞态。
        """
        return await self._governor.enforce_timeouts(running_snapshot, limit, now)

    async def _expire_stale_hitl(
        self,
        pending_snapshot: tuple[str, ...],
        ttl: int,
    ) -> int:
        """作废挂起超过 ``hitl_pending_ttl_seconds`` 的审批，返回作废数量。"""
        return await self._governor.expire_stale_hitl(pending_snapshot, ttl)

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

        # WHY 与活动时间同一个位置：两者都是「本轮已收尾」的动作，放在同一处才不会
        # 出现「时间刷新了、分支头没记」的半截状态。会话头只在当前分支恰好是最新那条
        # 时才等于分支头，切过分支之后就不再相等——不记下来，切回去会读到别人的内容。
        await self._branches.refresh_head(graph, handle)

        # WHY 出错后仍以 DONE 收尾而不直接结束：前端依赖 DONE 复位「正在输出的
        # 那条消息」，只有 ERROR 而没有 DONE 时，下一轮回复的文本会被追加到上
        # 一轮已经出错的气泡里。让 DONE 统一表示「流已关闭」，
        # 前端就不需要在两处分别处理结束条件。
        done_payload: dict[str, Any] = {"thread_id": thread_id}
        if handle.stop_requested:
            # WHY 用句柄上记录的原因而不是写死 ``"stopped"``：超时由后台协程置位，
            # 用户停止由 stop() 置位，两者都必须让前端看到「不是正常收尾」，
            # 但提示语不同（「已停止」vs「已超时终止」）。取不到原因时
            # 退化为 ``"stopped"``，避免出现没有 reason 的半截语义。
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

    def _tool_output_plan(self, handle: RunHandle) -> tuple[Path, str]:
        """本轮的留存方案：``(宿主存储目录, 虚拟根)``。

        WHY 两者一起给：落盘用宿主路径、引用（写进消息与事件）用虚拟路径，而「路径从哪来」
        只有 ``SessionRoot`` 知道（布局是它的事）。分开算迟早漂开——表现是前端点开「完整
        输出」时拿到 404，看起来像留存没写成功。

        Raises:
            RuntimeError: 句柄没有携带文件根（说明调用方绕过了会话解析）。
        """
        from config import SessionRoot

        scope = SessionRoot(self._config, self._root_of(handle))
        return scope.tool_output_store, scope.tool_outputs_virtual

    def _root_of(self, handle: RunHandle) -> Path:
        """本轮运行的文件根（用户工作空间或会话专属目录）。

        WHY 没有回落：句柄不带根说明调用方绕过了会话上下文的解析，而「拿不准写进哪个
        根」唯一安全的做法是报错——猜一个的结果是把工具输出留存写进别的项目，症状只是
        「完整输出」指向别处，不会报任何错。

        Raises:
            RuntimeError: 句柄没有携带文件根。
        """
        if not handle.workspace:
            raise RuntimeError(
                "运行句柄没有文件根：本轮运行的根由会话解析给出，缺失说明调用方绕过了它"
            )
        return Path(handle.workspace)

    async def _iterate(
        self,
        graph: Any,
        payload: Any,
        handle: RunHandle,
    ) -> AsyncIterator[AgentEvent]:
        """逐条翻译流增量，并负责运行期错误收敛。"""
        pending_outputs: list[tuple[Path, str]] = []
        output_sequence = itertools.count(1)
        # WHY 在这里取一次存储目录而不是每处现算：留存落盘与「完整输出」的虚拟路径必须
        # 来自同一个目录，两处各解析一次迟早会漂开——而漂开的表现是前端点开完整输出时
        # 拿到 404，看起来像留存没写成功。
        #
        # WHY 不再是「工作区根」：留存属于应用的数据（不是用户的项目文件），2026-09-21
        # 起落在根外存储里，由只读挂载 ``/_tool_outputs/`` 暴露给 Agent 回取。
        store_dir, virtual_root = self._tool_output_plan(handle)

        def capture_full_output(tool_name: str, full_text: str) -> str:
            """算出留存引用并暂存正文，返回供事件使用的虚拟路径。

            WHY 只暂存不写盘：本回调由翻译器**同步**调用，而写文件是阻塞 IO——
            在事件循环里直接写会让流式输出卡顿。落盘交给下面的 ``flush_outputs``。
            """
            path = tool_output_path(store_dir, handle.thread_id, next(output_sequence), tool_name)
            pending_outputs.append((path, full_text))
            return tool_output_virtual_path(virtual_root, handle.thread_id, path.name)

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

        actor_id = handle.actor_id or LOCAL_ACTOR_ID
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
            # WHY 必须显式传 context：长期记忆的命名空间在图内按主体计算，缺了它
            # 记忆会落进一个与面板读取时不同的命名空间——表现为「面板说没记住、Agent
            # 却照着做」，两边都不报错。
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
            # ``Task was destroyed but it is pending``。先 cancel 再逐一 await
            # 吸收结果，避免「异常从未被取回」的告警。
            for task in (chunk_task, stop_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (chunk_task, stop_task):
                if task is not None:
                    with suppress(BaseException):
                        await task

    # ------------------------------------------------------------------ 并发控制
    # 以下方法全部委托给 ``RunRegistry``：运行槽位、累计计数与审批挂起状态的唯一
    # 所有者在那边（锁与状态字段一并搬走）。这里保留同名方法，是为了让「谁能读、
    # 谁能改哪份状态」对调用方完全不变——指标端点、治理巡检与测试都按这些名字读。

    def _check_run_limits(self) -> None:
        """在真正触碰会话之前判定并发与限流。

        WHY 保留这一层而不是让调用方直接去问登记表：调用顺序（限流 → 读会话）
        本身是入口契约——限流必须早于「会话是否存在」的判断，否则被限流的一方能从
        404 与 429 的差别里推断出会话是否存在。这条约束写在调用顺序唯一确定的地方，
        最不容易被后来的改动破坏。

        Raises:
            RunRejectedError: 超出并发上限或被限流。
        """
        self._registry.check_limits("")

    def _acquire_run_slot(
        self,
        thread_id: str,
        *,
        model_name: str | None = None,
        owner_id: str = "",
        actor_id: str = "",
        fork_checkpoint: str = "",
        workspace: str = "",
    ) -> RunHandle:
        """占用该会话的运行槽位并登记运行句柄。

        Args:
            thread_id: 已规范化的会话 ID。
            model_name: 本轮使用的模型别名；``None`` 表示默认模型。
            owner_id: 会话所有者；本应用不区分用户，固定为空串。
            actor_id: 发起本轮运行的主体标识，用于工具审计归因。
            fork_checkpoint: 分叉起点检查点 id；空串表示接着当前分支的头。
            workspace: 本轮运行的工作区绝对路径；收尾阶段按它换算留存路径。

        Returns:
            本次运行的句柄；停止请求与运行指标都通过它传递。

        Raises:
            ValueError: ``thread_id`` 非法。
            ThreadBusyError: 该会话已有运行中的轮次。
            RunRejectedError: 并发上限在等待期间已被占满。
        """
        return self._registry.acquire(
            thread_id,
            model_name=model_name,
            owner_id=owner_id,
            actor_id=actor_id,
            fork_checkpoint=fork_checkpoint,
            workspace=workspace,
        )

    def release_run_slot(self, thread_id: str) -> None:
        """释放该会话的运行槽位。

        WHY 对外公开：生成器只会在被消费时通过 ``finally`` 释放槽位；若调用方拿到
        生成器后因异常未能消费（例如构造响应体时出错），槽位就再也没人释放，该会话
        会被永久判定为「运行中」。公开此方法让调用方能在这种情况下归还。

        Args:
            thread_id: 会话 ID。
        """
        self._registry.release(thread_id)

    def run_handle(self, thread_id: str) -> RunHandle | None:
        """返回指定会话的运行句柄；未在运行时为 ``None``。"""
        return self._registry.handle(thread_id)

    def is_running(self, thread_id: str) -> bool:
        """该会话当前是否有运行中的轮次。"""
        return self._registry.is_running(thread_id)

    def run_handles(self) -> dict[str, RunHandle]:
        """当前全部运行句柄的快照（会话 ID → 句柄）。"""
        return self._registry.handles()

    def running_thread_ids(self) -> tuple[str, ...]:
        """当前运行中的会话 ID 快照（供指标暴露）。"""
        return self._registry.running_ids()

    @property
    def started_runs(self) -> int:
        """进程启动以来累计发起的运行次数。"""
        return self._registry.started_runs

    def mark_hitl_pending(self, thread_id: str) -> None:
        """登记该会话有一个等待人工审批的中断，并记录挂起起始时刻。

        Args:
            thread_id: 已规范化的会话 ID。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        self._registry.mark_hitl_pending(thread_id)

    def clear_hitl_pending(self, thread_id: str) -> None:
        """清除该会话的待审批登记与过期标记；本就没有挂起时是 no-op。

        Args:
            thread_id: 会话 ID。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        self._registry.clear_hitl_pending(thread_id)

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
        return self._registry.expire_hitl_pending(thread_id)

    def is_hitl_expired(self, thread_id: str) -> bool:
        """该会话是否有一个已作废（超期）的审批挂起。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        return self._registry.is_hitl_expired(thread_id)

    def hitl_pending_age(self, thread_id: str) -> float | None:
        """该会话的审批已挂起秒数；未挂起时为 ``None``。"""
        return self._registry.hitl_pending_age(thread_id)

    def pending_hitl_thread_ids(self) -> tuple[str, ...]:
        """当前等待人工审批的会话 ID 快照。"""
        return self._registry.pending_hitl_ids()

    @property
    def timed_out_runs(self) -> int:
        """进程启动以来被运行超时强制取消的运行次数。"""
        return self._registry.timed_out_runs

    @property
    def expired_hitl(self) -> int:
        """进程启动以来因超期未决策而作废的审批挂起次数。"""
        return self._registry.expired_hitl

    @property
    def max_concurrent_runs(self) -> int:
        """配置的全局并发上限；``0`` 表示不限制。"""
        return self._registry.max_concurrent_runs

    @property
    def available_run_slots(self) -> int:
        """当前可用槽位数；上限为 ``0``（不限制）时返回 ``-1``。"""
        return self._registry.available_slots

    @property
    def rejected_runs(self) -> int:
        """进程启动以来因并发上限或限流被拒绝的运行次数。"""
        return self._registry.rejected_runs

    # ------------------------------------------------------------------ 元数据

    async def _record_turn(
        self,
        thread_id: str,
        *,
        title_hint: str | None,
        turn_delta: int,
        workspace: str = "",
        workspace_bound: bool = False,
        preset: str = "",
    ) -> dict[str, Any] | None:
        """把本轮对话登记到元数据表，并返回登记后的元数据。

        WHY 吞掉异常：对话本身已经完成，元数据只是列表展示用的旁路信息，
        让它把一次成功的交互变成错误响应是本末倒置；失败会留下完整日志供
        排查，返回 ``None`` 让调用方知道登记没成功。
        """
        try:
            return await self._thread_store.record_turn(
                thread_id,
                title_hint=self._build_title(title_hint),
                turn_delta=turn_delta,
                # WHY 在这条 UPSERT 里顺带写文件根：它只在会话还没有根时生效
                # （见 ``ThreadMetaStore.record_turn`` 的 CASE 分支），因此不会把已锁定的
                # 会话改到别处；而对还没有根的会话，这正是「根在第一次真正跑起来的那一刻
                # 锁定」的落点。
                workspace=workspace,
                workspace_bound=workspace_bound,
                # WHY 场景一起写：它决定技能视图里放哪些技能，而视图按**根**物化——
                # 场景必须在「根确定」的同一刻定下来，否则同一工作空间的两条会话会各自
                # 按不同场景重建视图，互相覆盖。
                preset=preset,
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
                # WHY 用量也要链路标识：审计说「谁在什么时候干了什么」，用量说
                # 「这次花了多少」——两者分开看都只是半张图，同一个 trace_id 才能
                # 回答「这一次请求到底花了多少」。CLI 形态下为 None，表示未知。
                trace_id=audit_trace_id(),
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


def _split_user_input(
    user_input: str | list[dict[str, Any]],
) -> tuple[str, str | list[dict[str, Any]]]:
    """把入口参数拆成「纯文本」与「真正进入图的内容」。

    WHY 需要两种形态：标题、日志与轮次判定要的都是文本，而带附件的消息必须是
    多模态内容块列表。只留文本会让附件丢失；只留列表会让标题变成
    ``[{'type': 'text', ...}]`` 这样的字符串——两者都不是「少个字段」，而是
    用户能直接看到的结果变错。

    Args:
        user_input: 纯文本，或由 ``AttachmentService`` 构造的内容块列表。

    Returns:
        ``(用于标题与日志的文本, 用于图的内容)``。

    Raises:
        ValueError: 两种形态都不含非空文本。
    """
    if isinstance(user_input, str):
        text = user_input.strip()
        if not text:
            raise ValueError("user_input 必须是非空字符串")
        return text, text

    if isinstance(user_input, list) and user_input:
        # WHY 从内容块里取文本而不是接受「只有图片」：本轮运行需要一段能当标题、
        # 能进日志、也能让模型理解意图的文本；纯图片消息会让会话标题为空、
        # 审计里也看不出用户要做什么。
        text = "".join(
            str(part.get("text", "")) for part in user_input if isinstance(part, dict)
        ).strip()
        if not text:
            raise ValueError("消息内容必须包含文本（图片不能单独成条）")
        return text, user_input

    raise ValueError("user_input 必须是非空字符串或多模态内容块列表")
