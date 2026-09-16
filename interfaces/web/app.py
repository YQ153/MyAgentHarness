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
from bootstrap.web import build_http_client, build_rate_limiter
from config import AppConfig
from interfaces.web.auth import router as auth_router
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

        logger.info("Web 服务启动完成：auth_mode=%s", config.auth_mode)
        try:
            yield
        finally:
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

    app.include_router(auth_router)
    app.include_router(router)

    if _STATIC_DIR.is_dir():
        # WHY 静态挂载必须放在路由注册之后：挂载 "/" 会吞掉之后注册的所有
        # 路径，导致 API 404。
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
    else:
        logger.warning("静态资源目录不存在，Web 界面不可用：%s", _STATIC_DIR)

    return app
