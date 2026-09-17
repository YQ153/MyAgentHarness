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
    build_http_client,
    build_rate_limiter,
    build_run_governance_worker,
)
from config import AppConfig
from interfaces.web.auth import router as auth_router
from interfaces.web.health import router as health_router
from interfaces.web.request_context import RequestContextMiddleware
from interfaces.web.routes import router

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
        # WHY http_client 与 rate_limiter 不放进 AppContext：它们只有 Web 形态
        # 需要，放进共享上下文会让 CLI 承担无谓的构造开销。
        http_client = build_http_client()
        rate_limiter = build_rate_limiter(config)

        # WHY 清理过期 device flow 记录：CLI 轮询产生的过期 code 若不清理，
        # 该表会随服务运行时长单调增长。
        try:
            await context.device_flow_store.cleanup()
        except Exception:
            logger.exception("device flow 过期记录清理失败，不影响服务启动")

        # WHY 整个启动段都包在 try/finally 里：http_client 与后台任务都在
        # yield 之前创建，若构造阶段抛错而不进 finally，这两者会连同已装配的
        # 连接一起泄漏。
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
            app.state.context = context
            app.state.http_client = http_client
            app.state.rate_limiter = rate_limiter
            app.state.api_key_store = context.api_key_store
            app.state.device_flow_store = context.device_flow_store
            app.state.threads = context.threads
            app.state.runs = context.runs
            app.state.catalog = context.catalog
            app.state.health = context.health
            app.state.usage = context.usage

            logger.info("Web 服务启动完成：auth_mode=%s", config.auth_mode)
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
            await http_client.aclose()
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
    app.include_router(auth_router)
    app.include_router(router)

    if _STATIC_DIR.is_dir():
        # WHY 静态挂载必须放在路由注册之后：挂载 "/" 会吞掉之后注册的所有
        # 路径，导致 API 404。
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
    else:
        logger.warning("静态资源目录不存在，Web 界面不可用：%s", _STATIC_DIR)

    return app
