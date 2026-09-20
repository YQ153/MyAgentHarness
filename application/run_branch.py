"""会话分叉：从某个历史检查点另起一条分支，再用一段文本跑一轮。

职责边界：只负责「定位分叉点、登记分支、占好槽位」这三件事，并把结果打包成
``ForkPlan``；事件流的产出仍归 ``RunService``（它才是事件协议的拥有者），所以本
模块不产出任何事件、也不消费图。

WHY 独立成模块：编辑与重新生成是同一条机制的两种参数，它们的实现加起来是
``run_service.py`` 里最大的一块（原先约 300 行），但没有任何一行与「发起对话、
恢复执行」共享——它只与历史检查点、分支记录有关。拆出去之后，运行主流程的
阅读者不必再跨过 ``aget_state_history`` 的消息链比对才能看到事件翻译。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from application.audit_recorder import AuditRecorder
from application.errors import NotFoundError
from application.message_utils import same_message_chain
from application.ports import ThreadMetadataStore
from application.run_registry import RunHandle, RunRegistry
from application.runnable import build_runnable_config

if TYPE_CHECKING:
    from agent.graph import AgentFactory
    from config import AppConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForkPlan:
    """一次分叉运行的全部要件：图、输入与已占好的槽位。

    WHY 返回计划而不是直接产出事件：事件流必须由 ``RunService._consume`` 消费，
    它同时负责用量落库、工具审计、槽位释放与分支头刷新。若由本模块自己产出事件，
    这条收尾链就要复制一份——而「收尾逻辑有两份」正是分支功能最容易出错的地方。
    """

    graph: Any
    payload: Any
    handle: RunHandle


class RunBranchService:
    """编辑 / 重新生成共用的分叉机制。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        thread_store: ThreadMetadataStore,
        graph_factory: AgentFactory,
        registry: RunRegistry,
        audit: AuditRecorder,
    ) -> None:
        """构造分叉服务。

        Args:
            config: 应用配置。
            thread_store: 会话元数据与分支记录存储。
            graph_factory: 图工厂；读历史与跑新分支都要取图。
            registry: 运行登记表，用于占用新分支的运行槽位。
            audit: 审计写入通道，取 ``RunService._audit``。

        Raises:
            ValueError: 任一必需依赖为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if thread_store is None:
            raise ValueError("thread_store 不能为 None")
        if graph_factory is None:
            raise ValueError("graph_factory 不能为 None")
        if registry is None:
            raise ValueError("registry 不能为 None")
        if audit is None:
            raise ValueError("audit 不能为 None")

        self._config = config
        self._thread_store = thread_store
        self._graph_factory = graph_factory
        self._registry = registry
        self._audit = audit

    # ------------------------------------------------------------------ 读

    async def messages_of(self, thread_id: str) -> list[Any]:
        """读当前分支的消息列表。

        WHY 读「当前分支」而不是会话最新状态：分叉之后两者不再是同一件事。用户在旧
        分支上点重新生成，理应接着**那条**分支的上下文，而不是最新那条的。

        Raises:
            NotFoundError: 该分支既不是当前分支、也没有记录在案。
            RuntimeError: 读取失败。
        """
        graph = self._graph_factory.get()
        branch = await self._thread_store.current_branch(thread_id)
        checkpoint = await self._head_of(thread_id, branch)

        try:
            state = await graph.aget_state(
                build_runnable_config(self._config, thread_id, checkpoint)
            )
        except Exception as exc:
            logger.exception("读取分支历史失败：thread=%s branch=%s", thread_id, branch)
            raise RuntimeError(f"读取分支历史失败：thread={thread_id}") from exc

        return list(getattr(state, "values", {}).get("messages") or [])

    async def _head_of(self, thread_id: str, branch: str) -> str | None:
        """返回某分支的头部检查点 id；``None`` 表示回落到会话当前的头。

        WHY 以分支记录里的头为准，而不是「当前分支就跟随会话的头」：这两个值只在
        「当前分支恰好是最新那条」时相等——而那正是最容易被当成恒等式的地方。用户一旦
        切回旧分支，会话头仍指向最新那条，按会话头去读会把旧分支显示成新分支的内容，
        而且界面上看不出错，只表现为「切了没反应」。

        Raises:
            NotFoundError: 该分支既不是当前分支、也没有记录在案。
        """
        record = await self._thread_store.get_branch(thread_id, branch)
        head = (record or {}).get("head_checkpoint") or ""
        if head:
            return head

        current = await self._thread_store.current_branch(thread_id)
        if branch == current:
            # 尚无记录（例如刚分叉出来、还没跑过的新分支）：此刻会话的头就是它的头
            return None

        raise NotFoundError("分支", branch)

    async def _live_head(self, graph: Any, thread_id: str) -> str:
        """读会话当前的头部检查点 id；空会话返回空串。"""
        state = await graph.aget_state(build_runnable_config(self._config, thread_id))
        return (
            (getattr(state, "config", None) or {})
            .get("configurable", {})
            .get("checkpoint_id", "")
            or ""
        )

    async def _freeze_unrecorded(self, graph: Any, thread_id: str, branch: str) -> None:
        """给「从未被离开过、因而没有头记录」的分支补记一次头。

        WHY 只在没有记录时补：一旦分支被离开过，它的头就已经由运行收尾或上一次补记
        准确落库了。此后会话头未必还属于它（可能属于更新的那条分支），用会话头去覆盖
        一份准确记录是错的。

        WHY「没有记录」时补记是可靠的：没有记录意味着这条分支从未被离开过，也就没有
        别的分支在它之后运行过——此刻的会话头正是它自己的头。归纳一下即可确认：任何
        一条分支一旦被离开就会被记上一次，于是「无记录」只可能是第一次离开。
        """
        if not branch and branch != "":
            return
        existing = await self._thread_store.get_branch(thread_id, branch)
        if (existing or {}).get("head_checkpoint"):
            return

        head = await self._live_head(graph, thread_id)
        if head:
            await self._thread_store.set_branch_head(thread_id, branch, head)

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
            if same_message_chain(values, wanted):
                return snapshot.config["configurable"]["checkpoint_id"]

        raise ValueError("找不到该轮次对应的分叉点，历史可能已被清理")

    # ------------------------------------------------------------------ 写

    async def refresh_head(self, graph: Any, handle: RunHandle) -> None:
        """把当前分支的头记成这一轮运行之后的头。

        WHY 必须持久化：上游的头是**会话级**的，永远指向最新那条分支；「我这条分支停在
        哪」则是分支级的。不记下来的话，切走再切回就找不回自己的位置。

        WHY 失败不上抛：记账失败不该把一次已经成功的对话变成错误；下一次运行会重试。
        """
        try:
            state = await graph.aget_state(
                build_runnable_config(self._config, handle.thread_id)
            )
            checkpoint = (
                (getattr(state, "config", None) or {})
                .get("configurable", {})
                .get("checkpoint_id", "")
            )
            if not checkpoint:
                return
            branch = await self._thread_store.current_branch(handle.thread_id)
            await self._thread_store.set_branch_head(handle.thread_id, branch, checkpoint)
        except Exception:
            logger.exception("刷新分支头失败：thread=%s", handle.thread_id)

    async def prepare_fork(
        self,
        thread_id: str,
        text: str,
        *,
        messages: list[Any],
        target_index: int,
        origin: str,
        label: str,
        model_name: str | None,
        owner_id: str,
        actor_id: str,
    ) -> ForkPlan:
        """从「目标消息出现之前」的检查点分叉，并登记好这一轮运行。

        调用方拿到计划后负责消费事件流并在收尾时释放槽位。

        Args:
            thread_id: 已规范化的会话 ID。
            text: 这一轮要跑的用户文本。
            messages: 当前分支的消息列表，用于定位分叉点。
            target_index: 目标消息在 ``messages`` 中的下标。
            origin: 分支来源标记（``edit`` / ``regenerate``）。
            label: 人可读的分支名。
            model_name: 模型别名；``None`` 表示默认模型。
            owner_id: 会话所有者；认证关闭时为空串。
            actor_id: 发起本轮运行的主体标识。

        Returns:
            分叉运行计划：图、输入与已占好的槽位。

        Raises:
            ValueError: 入参非法，或找不到分叉点（历史已被清理）。
            KeyError: 模型别名未注册。
            ThreadBusyError: 该会话已有运行中的轮次。
            RunRejectedError: 并发上限已满。
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text 必须是非空字符串")
        if not isinstance(target_index, int) or isinstance(target_index, bool):
            raise ValueError("target_index 必须是整数")
        if not 0 <= target_index < len(messages):
            raise ValueError(f"target_index 越界（{target_index} / {len(messages)}）")
        if not isinstance(origin, str) or not origin.strip():
            raise ValueError("origin 必须是非空字符串")

        # WHY 先取图再登记分支：解析模型别名与初始化模型可能失败，那属于「什么都没
        # 发生」；若先写了分支再失败，分支清单里会多出一条没有任何内容的分支。
        graph = self._graph_factory.get(model_name)

        fork_checkpoint = await self._find_fork_checkpoint(
            graph, thread_id, messages, target_index
        )

        # WHY 不无条件冻结旧分支的头：此刻会话的头属于**最新**那条分支，未必是正要
        # 离开的这一条——拿它去覆盖一份准确记录会把另一条分支的位置写进这一条。
        # 只在旧分支「从未被离开过」时补记（见 ``_freeze_unrecorded`` 的说明）。
        current = await self._thread_store.current_branch(thread_id)
        await self._freeze_unrecorded(graph, thread_id, current)

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
        self._registry.clear_hitl_pending(thread_id)
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

        handle = self._registry.acquire(
            thread_id,
            model_name=model_name,
            owner_id=owner_id,
            actor_id=actor_id,
            fork_checkpoint=fork_checkpoint,
        )

        payload: dict[str, Any] = {"messages": [{"role": "user", "content": text}]}
        return ForkPlan(graph=graph, payload=payload, handle=handle)
