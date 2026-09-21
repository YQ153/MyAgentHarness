"""长期记忆存储。

WHY 与检查点共用同一个 SQLite 文件：本项目的持久化栈就是「一个文件 +
aiosqlite」，记忆体量很小（用户偏好与项目约定），单开一个库只会多出一份
备份、迁移与连接生命周期的负担，而收益为零。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiosqlite
from langgraph.store.sqlite.aio import AsyncSqliteStore

from runtime.sqlite_lifecycle import open_sqlite_store

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


async def _prepare_memory_store(conn: aiosqlite.Connection) -> AsyncSqliteStore:
    """建表并返回长期记忆 Store；由 ``open_sqlite_store`` 在初始化阶段调用。

    WHY 显式 ``setup()``：建表虽幂等，但不建表时后续读写全部报「表不存在」，
    错误信息与真实原因（首次启动）相差很远。

    WHY 由 ``prepare`` 里构造而不是让调用方先建再传进来：``setup()`` 的异常必须
    落在 ``open_sqlite_store`` 的初始化捕获范围内，才会被记成
    "长期记忆初始化失败" —— 这一点此前是缺的（连接与 PRAGMA 都在捕获之外，
    真故障时反而一条记录都没有）。
    """
    store = AsyncSqliteStore(conn)
    await store.setup()
    return store


@asynccontextmanager
async def open_store(db_path: Path) -> AsyncIterator[AsyncSqliteStore]:
    """以异步上下文的方式提供长期记忆存储，退出时关闭连接。

    WHY 换掉 ``InMemoryStore``：它随进程消失，而 ``/memories/`` 路由与文档
    承诺的是「跨会话记忆」——重启即失让这个承诺只剩下 ``AGENTS.md`` 成立。
    SQLite 版 Store 由 ``langgraph-checkpoint-sqlite`` 附带，而本项目已依赖
    该包，因此这次替换是零新增依赖。

    WHY 必须是 ``AsyncSqliteStore``：图内的文件工具走异步 backend
    （``awrite`` / ``aread`` / ``als``），而该实现的同步接口在事件循环里会
    直接抛 ``InvalidStateError``（"Synchronous calls to AsyncSqliteStore
    detected in the main event loop"）——同步版 Store 在这里不是「慢」，
    而是会炸。

    WHY 自建连接而不直接 ``from_conn_string``：本函数需要显式设置两个 PRAGMA。
    WAL 是库级持久属性（通常已由检查点侧开启），但本模块不应假设初始化顺序；
    ``busy_timeout`` 更是连接级属性，缺了它会在与审计 / 用量表并发写入时
    立刻抛 "database is locked"，而 Agent 写记忆恰好发生在运行中途。
    连接本身由 ``open_sqlite_store`` 建立，本函数只声明差异点：

    - ``autocommit=True``（``isolation_level=None``）：与库自身的
      ``from_conn_string`` 口径一致，事务边界由 Store 内部管理；用默认的隐式
      事务会让它的显式事务互相嵌套。
    - ``row_factory=None``：不替它设 ``aiosqlite.Row``——行的取值方式由
      ``AsyncSqliteStore`` 自己管理，那属于上游实现细节。

    Args:
        db_path: SQLite 文件路径，父目录会自动创建。

    Yields:
        已完成建表的 ``AsyncSqliteStore``。

    Raises:
        ValueError: ``db_path`` 为 ``None``。
        Exception: 连接或建表失败时记日志后原样向上抛出，由调用方决定是否重试。
    """
    async with open_sqlite_store(
        db_path,
        label="长期记忆",
        prepare=_prepare_memory_store,
        row_factory=None,
        autocommit=True,
    ) as store:
        logger.info("长期记忆存储已就绪：%s（按主体隔离命名空间）", db_path)
        yield store
