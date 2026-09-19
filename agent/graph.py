"""Deep Agent 装配层——全应用唯一的 ``create_deep_agent`` 调用点。

收敛为单点的原因：``create_deep_agent`` 的参数组合会随 backend、permissions、
skills、profile 指数增长；散落多处调用必然导致护栏口径不一致，也让安全审计
失去锚点。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING

from deepagents import create_deep_agent
from langchain.agents.middleware import (
    ContextEditingMiddleware,
    ModelCallLimitMiddleware,
    TodoListMiddleware,
)
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from agent.backends import build_backend
from agent.guardrails import build_interrupt_on, build_permissions
from runtime.skill_view import sources_for_graph
from agent.profiles import ensure_profiles_registered
from agent.run_context import AgentRunContext
from llm.registry import ModelRegistry, build_default_registry

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore

    from config import AppConfig

logger = logging.getLogger(__name__)

_FALLBACK_SYSTEM_PROMPT = """你是通用任务助手，工作目录是受限虚拟文件系统。

超过 3 步的任务先用 write_todos 拆解计划，再逐步执行，并在完成后更新状态。
工具返回的大结果先 write_file 落盘，只把结论带回对话。
不要尝试读取 .env、密钥、证书类文件，这类访问会被安全规则直接拒绝。
回答使用简体中文。"""
"""兜底系统提示。

WHY 需要兜底：``system_prompt`` 传入 None 时 HarnessProfile 的
``base_system_prompt`` 仍会生效，但显式传入可以确保即使 Profile 未注册，
Agent 也保留最基本的规划与卸载引导。
"""


def get_registry(config: AppConfig) -> ModelRegistry:
    """按配置构造模型注册表（每次新建，内部共享模型缓存由 registry 负责）。"""
    if config is None:
        raise ValueError("config 不能为 None")
    ensure_profiles_registered()
    return build_default_registry(config)


def build_agent(
    config: AppConfig,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore,
    model_name: str | None = None,
    tools: Sequence[BaseTool] | None = None,
) -> CompiledStateGraph:
    """装配一个完整的 deep agent。

    Args:
        config: 应用配置，提供工作区、执行档位与护栏参数。
        checkpointer: 会话持久化实现；``None`` 时由调用方运行环境注入
            （例如 LangGraph Server 场景）。没有持久化则无法中断恢复。
        store: 长期记忆存储；**必填**。持久化实现由装配层决定
            （``runtime.store.open_store``）。
        model_name: 模型别名；``None`` 使用配置中的默认模型。
        tools: 扩展工具（自定义工具与 MCP 工具）；``None`` 表示不扩展。

    Returns:
        已编译的 LangGraph 图。

    Raises:
        ValueError: ``config`` 为 ``None``，或未提供 ``store``。
        RuntimeError: 模型初始化失败或装配过程出错。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    if store is None:
        # WHY 不再自建内存兜底：静默退回内存存储会让「长期记忆」在进程重启后
        # 悄悄消失，而开发期完全看不出来；装配层漏传时立刻失败才能被修掉。
        raise ValueError("store 不能为 None，长期记忆与 /memories/ 路由都依赖它")

    registry = get_registry(config)
    resolved_name = model_name or registry.default_name
    model = registry.get(resolved_name)

    backend = build_backend(config, store)

    # WHY 显式补充 TodoListMiddleware：deepagents 0.7.14 的默认中间件栈不含
    # 规划能力，通用长任务必须自己挂上，否则 Agent 容易在多步任务中迷失。
    middleware = [
        TodoListMiddleware(),
        # WHY ContextEditingMiddleware：DeepSeek 无 prompt 缓存收益，控制
        # 上下文成本只能靠裁剪历史的工具调用记录。
        ContextEditingMiddleware(),
        # WHY 限制单次运行调用次数：通用 Agent 最大的成本风险是模型陷入
        # 「读—改—再读」循环，必须有硬上限兜底。
        ModelCallLimitMiddleware(
            run_limit=config.max_model_calls_per_run,
            exit_behavior="end",
        ),
    ]

    # WHY 技能来源不直接用 ``config.skill_source_paths()``：技能的**启停**由数据库记录，
    # 而该配置只描述「技能包放在哪」。两者之间隔着一层派生产物——物化视图（见
    # ``runtime.skill_view``）：上游的来源必须是技能目录的**父目录**，无法逐技能过滤，
    # 所以「只加载启用的那些」只能靠派生出一份视图目录来达成。
    #
    # WHY 在这里判而不是在 bootstrap：``build_agent`` 是唯一真正消费来源的地方；把判定
    # 放到装配层，会让「视图丢了」与「技能全没了」之间的因果关系再隔一层。返回的告警
    # 走 WARNING，实际表现（退回配置目录 = 全部启用）与原因一起出现在日志里。
    skill_sources, skill_warning = sources_for_graph(config.workspace, config.skill_source_paths())
    if skill_warning:
        logger.warning(skill_warning)

    logger.info(
        "装配 Agent：model=%s mode=%s tier=%s skills=%d memory=%d tools=%d",
        resolved_name,
        config.execution_mode.value,
        config.sandbox_tier.value,
        len(skill_sources),
        len(config.memory_paths),
        len(tools or ()),
    )

    try:
        return create_deep_agent(
            model=model,
            system_prompt=_FALLBACK_SYSTEM_PROMPT,
            backend=backend,
            tools=list(tools) if tools else None,
            skills=skill_sources or None,
            memory=config.memory_paths or None,
            # WHY 传 backend 而不是直接取规则：可执行 backend 下工具级权限
            # 无法约束 execute，deepagents 会拒绝该组合；由 build_permissions
            # 按能力裁剪并告警，见其 docstring。
            permissions=build_permissions(backend),
            interrupt_on=build_interrupt_on(
                config.execution_mode,
                config.sandbox_tier,
                require_approval=config.sandbox_require_approval,
            ),
            middleware=middleware,
            checkpointer=checkpointer,
            store=store,
            # WHY 声明 context_schema：长期记忆按主体隔离，而命名空间是图内
            # 在调用时算出来的——主体只能经 ``Runtime.context`` 传进去。声明
            # 类型后，多传/漏传会在调用点就被发现，而不是表现为「记忆串味」。
            context_schema=AgentRunContext,
            name="universal-agent",
        )
    except Exception:
        logger.exception("Agent 装配失败：model=%s", resolved_name)
        raise


class AgentFactory:
    """按模型别名提供（并缓存）已装配的图。

    WHY 由工厂实例持有依赖，而不是模块级 dict 缓存：图的缓存键必须包含
    checkpointer 与 store——它们决定了会话状态与长期记忆落在哪。若用全局 dict
    只以模型名为键，换一组依赖后仍会返回先前的图，表现为「长期记忆串味」
    「对话状态读不到」这类难以定位的问题；而且全局缓存无法在测试之间隔离，
    也无法在配置变更后重建。

    WHY 缓存而非每次新建：``create_deep_agent`` 会重建整条中间件栈与工具集，
    开销可观；而模型切换只需要在首次切换时付一次代价。

    WHY 由调用方共享 store：``/memories/`` 路由绑定的是 Store 实例，若每个
    模型各持一份，用户在 A 模型下写入的长期记忆在 B 模型下就消失了。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        checkpointer: BaseCheckpointSaver | None = None,
        store: BaseStore,
        tools: Sequence[BaseTool] | None = None,
    ) -> None:
        """构造工厂。

        Args:
            config: 应用配置。
            checkpointer: 会话持久化实现；``None`` 时无持久化，中断恢复不可用。
            store: 长期记忆存储；**必填**，由装配层决定其实现在哪落盘。
            tools: 扩展工具；``None`` 表示不扩展。

        Raises:
            ValueError: ``config`` 或 ``store`` 为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if store is None:
            raise ValueError("store 不能为 None，长期记忆与 /memories/ 路由都依赖它")

        self._config = config
        self._checkpointer = checkpointer
        self._store: BaseStore = store
        # WHY 元组化：工具集在装配完成后不应再被就地增删，否则同一进程里
        # 先后装配的两张图会拿到不同的能力集。
        self._tools: tuple[BaseTool, ...] = tuple(tools or ())
        self._cache: dict[str, CompiledStateGraph] = {}
        # WHY 用锁而非直接依赖 GIL：``get`` 可能被多个 worker 线程并发调用，
        # 重复装配会浪费一次完整的中间件栈构建，也可能突破 provider 侧限流。
        self._lock = threading.Lock()

    @property
    def store(self) -> BaseStore:
        """本工厂共享的长期记忆存储，供需要直接读写 ``/memories/`` 的场景使用。"""
        return self._store

    def get(self, model_name: str | None = None) -> CompiledStateGraph:
        """取一个已装配的图，按模型别名缓存。

        Args:
            model_name: 模型别名；``None`` 表示使用配置中的默认模型。

        Returns:
            已编译的 LangGraph 图。

        Raises:
            KeyError: 模型别名未注册。
            RuntimeError: 模型初始化失败或装配过程出错。
        """
        resolved_name = model_name or self._config.default_model

        cached = self._cache.get(resolved_name)
        if cached is not None:
            return cached

        with self._lock:
            # 双检锁：快路径无锁返回，慢路径加锁后二次确认，避免重复装配
            cached = self._cache.get(resolved_name)
            if cached is not None:
                return cached

            agent = build_agent(
                self._config,
                checkpointer=self._checkpointer,
                store=self._store,
                model_name=resolved_name,
                tools=self._tools,
            )
            self._cache[resolved_name] = agent
            logger.info("已缓存 Agent 实例：model=%s", resolved_name)
            return agent
