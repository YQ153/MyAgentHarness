"""SQLite 存储的连接生命周期：7 个 ``open_*`` 的共用实现。

WHY 单独成模块：7 个存储的生命周期（校验路径 → 建目录 → 连接 + PRAGMA → 初始化 →
yield → 关闭）逐字同构，只有"建什么表、产出什么对象、日志叫什么"不同。各写一遍的
代价不只是行数——这份结构里出过一次真实缺陷：``yield`` 落在捕获初始化异常的 ``try``
里面，于是 ``async with`` 主体（调用方的装配或业务代码）抛出的异常被记成
「<某表>初始化失败」，一次配置错误被描述成一次数据库故障。修的时候六个文件各修一遍，
连解释原因的 WHY 注释也各写一份（同一段注释在 6 个文件里出现，实测）。

放在这里之后，这类结构只存在一处：**改一次就改完了**。回归用例见
``tests/runtime/test_open_store_contract.py``。

WHY 在 ``runtime`` 而不在 ``application`` / ``bootstrap``：它是基础设施层的内部实现
细节，而 ``runtime`` 是依赖图上的叶子（.importlinter 契约 3），谁都不依赖它，
放这里不会把生命周期语义泄漏到上层。

WHY ``prepare`` 是**单个**回调，而不是 ``setup`` + ``build`` 两个：知识库的初始化顺序
是"先建表、再按建表结果决定产出什么对象"（向量表有没有建成决定 ``KnowledgeStore`` 的
``vector_enabled``）。拆成两个回调就必须在它们之间传递中间状态，等于把一次初始化切开；
一个回调既够用，也不逼调用方改结构。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, TypeVar

import aiosqlite

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_StoreT = TypeVar("_StoreT")


@asynccontextmanager
async def open_sqlite_store(
    db_path: Path,
    *,
    label: str,
    prepare: Callable[[aiosqlite.Connection], Awaitable[_StoreT]],
    row_factory: Callable[..., Any] | None = aiosqlite.Row,
    autocommit: bool = False,
) -> AsyncIterator[_StoreT]:
    """打开一个 SQLite 存储：建立连接、跑一次初始化、交出对象、退出时关闭。

    WHY 用 ``yield`` 而不是返回一个需要手动关闭的对象：连接的存活期必须与调用方
    的 ``async with`` 一致，否则异常退出时连接会留到 GC 才释放，期间该文件可能
    一直持有写锁——这在并发写同一库的场景下表现为"偶发 database is locked"。

    Args:
        db_path: SQLite 文件路径，父目录会自动创建。
        label: 该存储在日志里的名字，用于拼出"<label>初始化失败 / 连接已关闭"。
            由调用方给而不是从模块名推导：日志要写给排障的人看，
            "会话元数据表"比 "thread_store" 更直接。
        prepare: 初始化回调，接收已连好的连接，负责建表 / 迁移 / 扩展加载等，
            并返回要交出的对象。**初始化失败要让异常冒出去**——本函数只负责把它
            记成"<label>初始化失败"并原样抛出，不改变异常类型。
        row_factory: 行的取值方式；``None`` 表示保持 aiosqlite 的默认（不设置）。

            WHY 需要这个开关：``AsyncSqliteStore`` 自己管理行的取值方式，
            替它设 ``aiosqlite.Row`` 属于猜测上游实现细节。
        autocommit: 是否以 ``isolation_level=None``（自动提交）连接。

            WHY 需要这个开关：``AsyncSqliteStore`` 自己管理事务边界，aiosqlite 的
            隐式事务会让它的显式事务互相嵌套，因此那一处必须自动提交；其余存储
            不需要该选项，也就不传。

    Yields:
        ``prepare`` 的返回值。

    Raises:
        ValueError: ``db_path`` 为 ``None``，或 ``label`` 为空。
        Exception: 连接失败、PRAGMA 失败或 ``prepare`` 抛出的异常，记日志后原样抛出。

    Note:
        ``yield`` 必须在捕获初始化异常的 ``try`` **之外**——这是本模块存在的直接
        原因（见模块 docstring）。改动本函数时，先跑
        ``tests/runtime/test_open_store_contract.py``：它有两条方向相反的断言，
        一条守"主体异常不得被误归因"，另一条守"真·初始化失败必须仍留记录"。
    """
    if db_path is None:
        raise ValueError("db_path 不能为 None")
    if not label:
        raise ValueError("label 不能为空")

    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn: aiosqlite.Connection | None = None
    try:
        # WHY 只把初始化包在捕获里：见模块 docstring。连接失败同样是初始化失败，
        # 因此连接与 PRAGMA 一并包在这个 try 里。
        try:
            if autocommit:
                conn = await aiosqlite.connect(str(db_path), isolation_level=None)
            else:
                conn = await aiosqlite.connect(str(db_path))
            if row_factory is not None:
                conn.row_factory = row_factory

            # WHY 显式设置 WAL（即便它通常是库级持久属性、已被别的存储开过）：
            # 本函数不应假设初始化顺序，各存储独立启用时行为要一致。
            await conn.execute("PRAGMA journal_mode=WAL;")
            # WHY busy_timeout：同一个库上有多个存储并发写入（检查点、审计、用量、
            # 长期记忆），默认行为是立即返回 "database is locked"；
            # 等待几秒远比报错合理，而 Agent 的记忆写入恰好发生在运行中途。
            await conn.execute("PRAGMA busy_timeout=5000;")

            store = await prepare(conn)
        except Exception:
            logger.exception("%s初始化失败：%s", label, db_path)
            raise
        yield store
    finally:
        # WHY 判空：连接可能从未建立成功（上面第一条语句就抛了），此时 ``conn`` 仍是
        # ``None``——原结构会在这里 ``None.close()``，用一个次生异常盖住真正的原因。
        if conn is not None:
            await conn.close()
            # WHY 关闭日志是 DEBUG：它是诊断细节，而"已就绪"是启动横幅（由各调用方
            # 自己记 INFO）。7 个存储在退出时各留一条 INFO 会把退出日志占满，
            # 需要时把日志级别调到 DEBUG 即可拿到。
            logger.debug("%s连接已关闭：%s", label, db_path)
