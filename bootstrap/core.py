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
from application.session_registry import SessionRegistry
from bootstrap.context import AppContext
from config import AppConfig
from knowledge_runtime import close_service, ensure_service
from runtime.audit_store import open_audit_store
from runtime.checkpointer import checkpointer_context
from runtime.skill_store import open_skill_store
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
        open_usage_store(config.db_path) as usage_store,
        # 技能启停状态与其余元数据同库：它没有独立的生命周期诉求（不像知识库要加载
        # 向量扩展、且要能整库重建），故沿用「一个 db_path 装全部元数据」的既有约定。
        open_skill_store(config.db_path) as skill_store,
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

        # WHY 注册表只构造一次：目录与就绪探测都只需要读它的规格清单，
        # 构造两份既浪费一次规格解析，也让两处看到不同的默认模型视图。
        registry = get_registry(config)

        # WHY 工作区注册表在图形工厂之前装配、并立刻装配默认工作区：
        #
        # - 图的技能来源指向工作区里的物化视图（``/.skills-active``），而那份派生物由
        #   ``SkillService.refresh_view`` 重建。它必须**在任何图被装配之前**完成，否则
        #   新建会话会加载到过时的技能集——而技能索引每会话只加载一次，错了不会自愈。
        # - 重建失败要拦住启动（不吞掉）：视图建不出来时 ``sources_for_graph`` 会退回
        #   「全部技能」，于是「面板说某技能已停用、Agent 却照用」会同时成立。宁可起不来，
        #   也不要一个界面与实际互相矛盾的实例。
        #
        # WHY 启动时一个根都不装配：根由会话在创建/首轮交互时确定，启动时不存在「当前
        # 根」这种东西。装配是按根惰性发生的（见 ``SessionRegistry.services``）——提前
        # 替用户还没用过的项目建目录、开 SQLite 连接，是替他们做决定。
        workspaces = SessionRegistry(
            config,
            thread_store=thread_store,
            skill_store=skill_store,
            model_registry=registry,
            # WHY 用回调注入知识库的装配入口：``knowledge_runtime`` 是根级模块，且它
            # 反过来导入 ``application.knowledge_service``；应用层直接依赖它会接成一个环。
            knowledge_provider=lambda scope: ensure_service(config, scope=scope),
            audit_store=audit_store,
        )

        graph_factory = AgentFactory(
            config,
            checkpointer=checkpointer,
            store=store,
            tools=tool_bundle.tools,
        )

        catalog = ModelCatalog(registry)
        runs = RunService(
            config,
            thread_store=thread_store,
            graph_factory=graph_factory,
            workspaces=workspaces,
            audit_store=audit_store,
            usage_store=usage_store,
            tool_catalog=tool_catalog,
        )

        context = AppContext(
            config=config,
            checkpointer=checkpointer,
            thread_store=thread_store,
            audit_store=audit_store,
            graph_factory=graph_factory,
            threads=ThreadService(
                config,
                checkpointer=checkpointer,
                thread_store=thread_store,
                graph_factory=graph_factory,
                workspaces=workspaces,
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
            # WHY 只挂注册表、不挂任何「已装配好的会话服务」：文件面板、附件、技能视图
            # 与知识库都是**按根**各有一份的，而启动时还没有任何根。把某一份摆在这里，
            # 等于给「顺手用全局那个」留一条捷径——而它的症状是「B 会话的面板显示 A
            # 项目的文件」，两边都不报错。
            workspaces=workspaces,
        )

        logger.info(
            "核心依赖装配完成：db=%s sessions_root=%s tools=%d",
            config.db_path,
            config.resolved_sessions_root,
            len(tool_bundle.tools),
        )
        # 注：知识库的向量能力不再在这里报告——它按根各有一份，启动时一个都没装配。
        # 该事实由 ``GET /api/knowledge``（按会话）如实回答，那里才是它的归属处。
        try:
            yield context
        finally:
            # WHY 单独关闭知识库：它的连接由 knowledge_runtime 的退出栈持有（工具与
            # 接口要用同一份实例），不在上面的 ``async with`` 组里。放在 finally 里，
            # 异常退出时也能收干净。
            await close_service()
            logger.info("核心依赖已释放")
