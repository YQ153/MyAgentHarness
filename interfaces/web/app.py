"""FastAPI 应用工厂。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agent.graph import AgentFactory, get_registry
from application.model_catalog import ModelCatalog
from application.rate_limiter import RateLimiter
from application.run_service import RunService
from application.thread_service import ThreadService
from interfaces.web.auth import router as auth_router
from interfaces.web.routes import router
from runtime.api_key_store import open_api_key_store
from runtime.audit_store import open_audit_store
from runtime.checkpointer import checkpointer_context
from runtime.device_flow_store import open_device_flow_store
from runtime.thread_store import open_thread_store

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """管理应用级资源的生命周期。

    WHY 放在 lifespan 而不是模块导入时：服务构造会打开 SQLite 连接并初始化
    模型，放到这里才能保证进程退出时一定走到清理逻辑（哪怕是异常退出）。
    """
    config: AppConfig = app.state.config
    # WHY 用上下文管理器托管连接：它们的生命周期都与应用一致，
    # 只有放在这里才能保证正常退出与异常退出时连接都被关闭。
    # http_client 供 OIDC 流程使用，与数据库连接一起开闭。
    http_client = httpx.AsyncClient(timeout=15.0, follow_redirects=False)
    rate_limiter = RateLimiter(
        window_seconds=config.auth_rate_limit_window_seconds,
        max_attempts=config.auth_rate_limit_max_attempts,
    )
    async with (
        checkpointer_context(config.db_path) as checkpointer,
        open_thread_store(config.db_path) as thread_store,
        open_audit_store(config.db_path) as audit_store,
        open_api_key_store(config.db_path) as api_key_store,
        open_device_flow_store(config.db_path) as device_flow_store,
    ):
        # WHY 由工厂统一持有长期记忆存储：``/memories/`` 路由绑定的是 Store
        # 实例，若每个模型各持一份，用户在 A 模型下写入的长期记忆在 B 模型下
        # 就消失了。
        graph_factory = AgentFactory(config, checkpointer=checkpointer)

        app.state.http_client = http_client
        app.state.rate_limiter = rate_limiter
        app.state.api_key_store = api_key_store
        app.state.device_flow_store = device_flow_store
        await device_flow_store.cleanup()
        app.state.threads = ThreadService(
            config,
            checkpointer=checkpointer,
            thread_store=thread_store,
            graph_factory=graph_factory,
            audit_store=audit_store,
        )
        app.state.runs = RunService(
            config,
            thread_store=thread_store,
            graph_factory=graph_factory,
            audit_store=audit_store,
        )
        app.state.catalog = ModelCatalog(get_registry(config))

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
