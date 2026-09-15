"""对话状态持久化。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@asynccontextmanager
async def checkpointer_context(db_path: Path) -> AsyncIterator[AsyncSqliteSaver]:
    """以异步上下文的方式提供检查点保存器，退出时自动关闭连接。

    WHY 必须用 ``AsyncSqliteSaver``：图的运行入口是 ``astream``（异步），
    LangGraph 内部会调用 checkpointer 的异步接口；同步版 ``SqliteSaver`` 在此
    直接抛 ``NotImplementedError``，表现为「第一轮对话必定崩溃」。

    WHY 用上下文管理器持有而不是自己 new 连接：``from_conn_string`` 返回的是
    异步上下文管理器，连接建立与关闭都由它负责；调用方另行持有连接不仅重复，
    还容易在异常路径下泄漏。

    Yields:
        已完成建表的 ``AsyncSqliteSaver`` 实例。

    Raises:
        ValueError: ``db_path`` 为 None。
        Exception: 建表失败时原样向上抛出，由调用方决定是否重试。
    """
    if db_path is None:
        raise ValueError("db_path 不能为 None")

    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        try:
            # WHY setup 必须显式调用：建表虽幂等，但首次运行时不建表会让
            # 后续 aget/aput 全部报「表不存在」，错误信息与真实原因相差很远。
            await saver.setup()
        except Exception:
            logger.exception("检查点表初始化失败：%s", db_path)
            raise

        logger.info("检查点已就绪：%s", db_path)
        yield saver

    logger.info("检查点连接已关闭：%s", db_path)
