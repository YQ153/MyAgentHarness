"""Deep Agent 装配层——全应用唯一的 ``create_deep_agent`` 调用点。

收敛为单点的原因：``create_deep_agent`` 的参数组合会随 backend、permissions、
skills、profile 指数增长；散落多处调用必然导致护栏口径不一致，也让安全审计
失去锚点。
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from deepagents import create_deep_agent
from langchain.agents.middleware import (
    ContextEditingMiddleware,
    ModelCallLimitMiddleware,
    TodoListMiddleware,
)
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.memory import InMemoryStore

from agent.backends import build_backend
from agent.guardrails import build_interrupt_on, build_permissions
from agent.profiles import ensure_profiles_registered
from llm.registry import ModelRegistry, build_default_registry
from runtime.store import build_store

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
    store: BaseStore | None = None,
    model_name: str | None = None,
) -> CompiledStateGraph:
    """装配一个完整的 deep agent。

    Args:
        config: 应用配置，提供工作区、执行档位与护栏参数。
        checkpointer: 会话持久化实现；``None`` 时由调用方运行环境注入
            （例如 LangGraph Server 场景）。没有持久化则无法中断恢复。
        store: 长期记忆存储；``None`` 时新建进程内存储。
        model_name: 模型别名；``None`` 使用配置中的默认模型。

    Returns:
        已编译的 LangGraph 图。

    Raises:
        RuntimeError: 模型初始化失败或装配过程出错。
    """
    if config is None:
        raise ValueError("config 不能为 None")

    registry = get_registry(config)
    resolved_name = model_name or registry.default_name
    model = registry.get(resolved_name)

    effective_store: BaseStore = store if store is not None else build_store(config)
    backend = build_backend(config, effective_store)

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

    logger.info(
        "装配 Agent：model=%s mode=%s skills=%d memory=%d",
        resolved_name,
        config.execution_mode.value,
        len(config.skill_source_paths()),
        len(config.memory_paths),
    )

    try:
        return create_deep_agent(
            model=model,
            system_prompt=_FALLBACK_SYSTEM_PROMPT,
            backend=backend,
            skills=config.skill_source_paths() or None,
            memory=config.memory_paths or None,
            permissions=build_permissions(),
            interrupt_on=build_interrupt_on(config.execution_mode),
            middleware=middleware,
            checkpointer=checkpointer,
            store=effective_store,
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
        store: BaseStore | None = None,
    ) -> None:
        """构造工厂。

        Args:
            config: 应用配置。
            checkpointer: 会话持久化实现；``None`` 时无持久化，中断恢复不可用。
            store: 长期记忆存储；``None`` 时新建进程内存储。

        Raises:
            ValueError: ``config`` 为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")

        self._config = config
        self._checkpointer = checkpointer
        self._store: BaseStore = store if store is not None else build_store(config)
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
            )
            self._cache[resolved_name] = agent
            logger.info("已缓存 Agent 实例：model=%s", resolved_name)
            return agent
