"""FastAPI 应用工厂。

职责边界：只做「协议适配」——注册路由、挂载静态资源、托管 Web 专有资源的
生命周期。对象组装全部交给 ``bootstrap`` 层，因此本模块不直接依赖 ``runtime``。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from bootstrap.core import build_app_context
from bootstrap.web import (
    build_audit_retention_worker,
    build_run_governance_worker,
)
from config import AppConfig
from interfaces.web.attachment_routes import router as attachment_router
from interfaces.web.health import router as health_router
from interfaces.web.knowledge_routes import router as knowledge_router
from interfaces.web.request_context import RequestContextMiddleware
from interfaces.web.routes import router
from interfaces.web.skill_routes import router as skill_router
from interfaces.web.workspace_routes import router as workspace_router

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """管理应用级资源的生命周期。

    WHY 组装放在 lifespan 内而非 ``create_app``：store 与 checkpointer 的构造
    需要异步上下文（``async with``），而 ``create_app`` 是同步函数。放在这里
    既能托管生命周期，也能保证进程退出时一定走到清理逻辑（含异常退出）。
    """
    config: AppConfig = app.state.config

    async with build_app_context(config) as context:
        # WHY 整个启动段都包在 try/finally 里：后台任务在 yield 之前创建，
        # 若构造阶段抛错而不进 finally，它们会连同已装配的连接一起泄漏。
        retention_worker = None
        governance_worker = None
        try:
            # WHY 只在 Web 形态启动保留清理：CLI 是一次性进程，跑一个常驻清理
            # 协程既无收益也会拖慢退出。清理任务会先立即执行一次，再进入周期。
            retention_worker = build_audit_retention_worker(config, context.audit_store)
            try:
                retention_worker.start()
            except Exception:
                # WHY 不阻断启动：审计清理是运维旁路能力，失败时保留全量日志
                # 远好于让服务起不来；失败原因已记日志，可另行告警。
                logger.exception("审计保留清理任务启动失败，审计日志将不再自动归档清理")

            # WHY 运行治理同样只在长驻形态启动：运行超时与审批挂起 TTL 都是
            # 「随时间推移才会触发」的收口，一次性进程跑不到那一刻。
            governance_worker = build_run_governance_worker(config, context.runs)
            try:
                governance_worker.start()
            except Exception:
                logger.exception("运行治理任务启动失败，超时运行与超期审批将不再自动收口")

            # 路由层通过 ``app.state`` 取依赖；这里把 AppContext 的内容铺开，
            # 保持既有路由代码不变。
            # WHY 凡是路由会读的依赖都必须在这里铺开：漏掉一项时，宽容的读取点
            # （``getattr(state, name, None)``）会静默降级，而严格的读取点
            # （``state.audit_store``）会在真机上直接 500。
            # 这条约束由 tests/interfaces/web/test_app_state_contract.py 静态兜住。
            app.state.audit_store = context.audit_store
            app.state.context = context
            app.state.threads = context.threads
            # WHY 只挂注册表、不挂它按启动默认值装配的那四类服务：工作区在会话级可选
            # 之后，「文件面板 / 附件 / 技能视图 / 知识库」都是**按会话**各有一份的。
            # 把默认工作区那一份摆在这里，等于给后续代码留了一条「顺手用全局那个」的
            # 捷径——而它的症状是「B 会话的面板显示 A 项目的文件」，两边都不报错。
            # 一律经 ``workspaces.services_for(...)`` 取，取错就是 AttributeError。
            app.state.workspaces = context.workspaces
            app.state.runs = context.runs
            app.state.catalog = context.catalog
            app.state.health = context.health
            app.state.usage = context.usage
            app.state.tools = context.tools
            app.state.memories = context.memories

            logger.info("Web 服务启动完成：host=%s", config.host)
            yield
        finally:
            # WHY 先停后台任务再关连接：清理任务持有 audit_store 连接，
            # 顺序颠倒会让它在关闭的连接上执行 DELETE。
            # WHY 治理任务先于清理任务停止：治理会写审计事件，若先停下清理
            # 任务无所谓，但若先关掉连接就会让最后一轮巡检半途报错。
            if governance_worker is not None:
                try:
                    await governance_worker.stop()
                except Exception:
                    logger.exception("运行治理任务停止失败")
            if retention_worker is not None:
                try:
                    await retention_worker.stop()
                except Exception:
                    logger.exception("审计保留清理任务停止失败")
            # WHY 这里不再关闭其它共享资源：本行原先调用一个关闭 OIDC httpx 客户端的
            # 辅助函数，OIDC 移除后那个客户端与函数一起消失了，调用点却留了下来——
            # 结果是**进程退出必失败**（NameError），而日志会把它显示成「Web 服务已停止」
            # 之前的一堆存储初始化失败，把排查引向无关方向。两个后台任务已在上面显式
            # 收尾，它们与各存储的连接由 ``build_app_context`` 退出时统一关闭（在本次
            # finally 之后发生）。若将来新增需要显式关闭的资源，请在此**就地**关闭并
            # 写明理由，不要引入一个跨模块的“统一清理”间接层。
            logger.info("Web 服务已停止")


def create_app(config: AppConfig) -> FastAPI:
    """构造 FastAPI 应用。

    Args:
        config: 应用配置，会被挂到 ``app.state.config`` 供 lifespan 取用。

    Returns:
        已装配路由与静态资源的应用实例。

    Raises:
        ValueError: ``config`` 为 ``None``。
    """
    if config is None:
        raise ValueError("config 不能为 None")

    app = FastAPI(
        title="通用 Agent",
        description="基于 deepagents 的通用任务助手，支持 CLI 与 Web 双形态。",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.state.config = config

    # WHY 审计上下文中间件最先注册：Starlette 的中间件按注册顺序由外向内执行，
    # 最先注册即最外层，路由与异常处理都在它之内，任何分支写下的审计都能
    # 读到 IP/UA（包括鉴权失败这类在下游就被拦截的请求）。
    app.add_middleware(RequestContextMiddleware)

    # WHY 运维路由先注册：它们不依赖任何业务状态，注册在最前面可以保证
    # 启动阶段（业务路由尚未就绪）探活请求仍能被应答，而不是被后面的
    # 静态挂载吞成 404。
    app.include_router(health_router)
    app.include_router(router)
    app.include_router(workspace_router)
    app.include_router(attachment_router)
    app.include_router(knowledge_router)
    app.include_router(skill_router)

    if _STATIC_DIR.is_dir():
        # WHY 静态挂载必须放在路由注册之后：挂载 "/" 会吞掉之后注册的所有
        # 路径，导致 API 404。
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
    else:
        logger.warning("静态资源目录不存在，Web 界面不可用：%s", _STATIC_DIR)

    return app
