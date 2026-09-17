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

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


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

    WHY 自建连接而不直接 ``from_conn_string``：两个 PRAGMA 必须显式设置。
    WAL 是库级持久属性（通常已由检查点侧开启），但本模块不应假设初始化顺序；
    ``busy_timeout`` 更是连接级属性，缺了它会在与审计 / 用量表并发写入时
    立刻抛 "database is locked"，而 Agent 写记忆恰好发生在运行中途。

    Args:
        db_path: SQLite 文件路径，父目录会自动创建。

    Yields:
        已完成建表的 ``AsyncSqliteStore``。

    Raises:
        ValueError: ``db_path`` 为 ``None``。
        Exception: 建表失败时原样向上抛出，由调用方决定是否重试。
    """
    if db_path is None:
        raise ValueError("db_path 不能为 None")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：与库自身的 from_conn_string 口径一致（自动提交），
    # 事务边界由 Store 内部管理；用默认的隐式事务会让它的显式事务互相嵌套。
    conn = await aiosqlite.connect(str(db_path), isolation_level=None)
    try:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        store = AsyncSqliteStore(conn)
        try:
            # WHY 显式 setup：建表虽幂等，但不建表时后续读写全部报
            # 「表不存在」，错误信息与真实原因（首次启动）相差很远。
            await store.setup()
        except Exception:
            logger.exception("长期记忆表初始化失败：%s", db_path)
            raise
        logger.info("长期记忆存储已就绪：%s（按主体隔离命名空间）", db_path)
        yield store
    finally:
        # WHY 用 finally 而不是只依赖正常退出：装配失败、客户端异常退出
        # 都要走到这里，否则连接会留到 GC 才关闭，期间该文件可能一直持有锁。
        await conn.close()
        logger.info("长期记忆连接已关闭：%s", db_path)
