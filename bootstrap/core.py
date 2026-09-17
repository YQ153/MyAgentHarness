"""核心依赖组装：CLI 与 Web 共用同一套装配逻辑。

WHY 收敛为单点：装配逻辑一旦出现两份副本，新增一个 store 参数就必须同步
修改两处，任何一处遗漏都会造成「Web 有审计、CLI 无审计」这类安全盲区，
而这类差异难以通过功能测试发现。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agent.graph import AgentFactory, get_registry
from agent.profiles import ensure_profiles_registered
from agent.tooling import build_tool_bundle
from application.health import HealthService
from application.memory_service import MemoryService
from application.model_catalog import ModelCatalog
from application.run_service import RunService
from application.thread_service import ThreadService
from application.tool_catalog import ToolCatalog
from application.usage_service import UsageService
from bootstrap.context import AppContext
from config import AppConfig
from runtime.api_key_store import open_api_key_store
from runtime.audit_store import open_audit_store
from runtime.checkpointer import checkpointer_context
from runtime.device_flow_store import open_device_flow_store
from runtime.store import open_store
from runtime.thread_store import open_thread_store
from runtime.usage_store import open_usage_store

logger = logging.getLogger(__name__)


@asynccontextmanager
async def build_app_context(config: AppConfig) -> AsyncIterator[AppContext]:
    """组装跨形态共享的核心依赖。

    WHY 用上下文管理器：store 与 checkpointer 的生命周期必须与进程一致，
    只有交给 ``async with`` 才能保证正常退出与异常退出时连接都被关闭，
    而不是依赖 GC 的回收时机。

    Args:
        config: 应用配置。

    Yields:
        AppContext: 已装配完成的依赖集合。

    Raises:
        ValueError: ``config`` 为 ``None``。
    """
    if config is None:
        raise ValueError("config 不能为 None")

    # WHY 在此注册 HarnessProfile：注册属于装配职责。放在 ``llm`` 包内会让
    # ``llm`` 反向依赖 ``agent`` 形成包级循环；放在此处后依赖方向保持单向。
    ensure_profiles_registered()

    async with (
        checkpointer_context(config.db_path) as checkpointer,
        open_thread_store(config.db_path) as thread_store,
        open_audit_store(config.db_path) as audit_store,
        open_api_key_store(config.db_path) as api_key_store,
        open_device_flow_store(config.db_path) as device_flow_store,
        open_usage_store(config.db_path) as usage_store,
        # WHY 记忆存储也走 ``async with``：它的连接生命周期必须与进程一致，
        # 否则退出时连接留到 GC 才释放，期间该 SQLite 文件可能一直持有锁。
        open_store(config.db_path) as store,
    ):
        # WHY 工具在装配图之前装好：工具集是图的一部分，图一旦缓存就不会
        # 再读它；放到后面会造成「首个请求没工具、之后突然有了」这种差异。
        # WHY 装配失败要让进程起不来（除 MCP 降级外）：工具名冲突与模块
        # 导入失败都是配置错误，带着一个「少了工具」的 Agent 继续服务，
        # 只会把失败推迟到某次具体对话。
        tool_bundle = await build_tool_bundle(config)
        tool_catalog = ToolCatalog(tool_bundle)
        graph_factory = AgentFactory(
            config,
            checkpointer=checkpointer,
            store=store,
            tools=tool_bundle.tools,
        )

        # WHY 注册表只构造一次：目录与就绪探测都只需要读它的规格清单，
        # 构造两份既浪费一次规格解析，也让两处看到不同的默认模型视图。
        registry = get_registry(config)
        catalog = ModelCatalog(registry)
        runs = RunService(
            config,
            thread_store=thread_store,
            graph_factory=graph_factory,
            audit_store=audit_store,
            usage_store=usage_store,
            tool_catalog=tool_catalog,
        )

        context = AppContext(
            config=config,
            checkpointer=checkpointer,
            thread_store=thread_store,
            audit_store=audit_store,
            api_key_store=api_key_store,
            device_flow_store=device_flow_store,
            graph_factory=graph_factory,
            threads=ThreadService(
                config,
                checkpointer=checkpointer,
                thread_store=thread_store,
                graph_factory=graph_factory,
                audit_store=audit_store,
            ),
            runs=runs,
            catalog=catalog,
            # WHY 健康检查复用同一个 run_service 实例：它的运行登记与挂起审批
            # 集合就是指标的真相来源，另建一份只会读到永远为 0 的计数。
            health=HealthService(
                config,
                thread_store=thread_store,
                run_service=runs,
                audit_store=audit_store,
                catalog=catalog,
            ),
            usage_store=usage_store,
            usage=UsageService(
                config,
                usage_store=usage_store,
                thread_store=thread_store,
            ),
            tools=tool_catalog,
            # WHY 与图共享同一个 store 实例：管理面板与 Agent 必须看到同一份
            # 记忆——各持一份（哪怕指向同一文件）会让「面板显示已删除」与
            # 「Agent 还记得」同时成立。
            memories=MemoryService(config, store=store, audit_store=audit_store),
        )

        logger.info(
            "核心依赖装配完成：db=%s auth_mode=%s tools=%d",
            config.db_path,
            config.auth_mode,
            len(tool_bundle.tools),
        )
        yield context
        logger.info("核心依赖已释放")
