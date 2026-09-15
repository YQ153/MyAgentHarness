"""FastAPI 应用工厂。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agent.graph import AgentFactory, get_registry
from application.model_catalog import ModelCatalog
from application.run_service import RunService
from application.thread_service import ThreadService
from interfaces.web.routes import router
from runtime.checkpointer import checkpointer_context
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
    # WHY 用上下文管理器托管两类连接：二者的生命周期都与应用一致，
    # 只有放在这里才能保证正常退出与异常退出时连接都被关闭。
    async with (
        checkpointer_context(config.db_path) as checkpointer,
        open_thread_store(config.db_path) as thread_store,
    ):
        # WHY 由工厂统一持有长期记忆存储：``/memories/`` 路由绑定的是 Store
        # 实例，若每个模型各持一份，用户在 A 模型下写入的长期记忆在 B 模型下
        # 就消失了。
        graph_factory = AgentFactory(config, checkpointer=checkpointer)

        app.state.threads = ThreadService(
            config,
            checkpointer=checkpointer,
            thread_store=thread_store,
            graph_factory=graph_factory,
        )
        app.state.runs = RunService(
            config,
            thread_store=thread_store,
            graph_factory=graph_factory,
        )
        app.state.catalog = ModelCatalog(get_registry(config))

        logger.info("Web 服务启动完成")
        try:
            yield
        finally:
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

    app.include_router(router)

    if _STATIC_DIR.is_dir():
        # WHY 静态挂载必须放在路由注册之后：挂载 "/" 会吞掉之后注册的所有
        # 路径，导致 API 404。
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
    else:
        logger.warning("静态资源目录不存在，Web 界面不可用：%s", _STATIC_DIR)

    return app
