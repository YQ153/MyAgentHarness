"""知识库的进程级句柄与惰性装配。

WHY 需要一个进程级句柄，而不是在每处各自装配：

- 自定义工具扩展点（``CUSTOM_TOOL_MODULES``）只把 ``config`` 交给工具模块，**没有**
  注入依赖的通道；而知识库持有的是一条 SQLite 连接（打开是异步的、生命周期应与
  进程一致），不可能每次工具调用都开一份。
- 于是装配只做一次、放在这里：``bootstrap`` 在启动时建起来并负责关闭，工具与接口
  拿到的是同一个实例——与「图与记忆面板共享同一个 store」同一个理由，各持一份会让
  两边看到不同的事实。

WHY 不做成导入期就构造的模块级单例：它需要 ``await``；而且构造失败应当落在**启动
路径**上（能被看见、能拦住进程），而不是发生在某个模块被 import 的那一刻。

数据库落在 ``<数据目录>/knowledge.db``，与检查点等库**分开**：

- 向量维度或模型一变就必须整库重建，独立文件让「删掉重来」是一条明确可执行的指令；
- ``vec0`` 是加载式扩展，把它写进主库会让「扩展在当前环境不可用」与「检查点库」
  纠缠在一起——那两件事的处置方式完全不同。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from application.knowledge_service import KnowledgeService
from llm.embeddings import build_embeddings
from runtime.knowledge_store import open_knowledge_store

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

KNOWLEDGE_DB_NAME = "knowledge.db"
"""知识库文件名；放在数据目录下，与 ``db_path`` 同处一块可写卷。"""

_service: KnowledgeService | None = None
_stack: AsyncExitStack | None = None
_lock = asyncio.Lock()


def knowledge_db_path(config: AppConfig) -> Path:
    """返回知识库文件路径。

    WHY 由 ``db_path`` 的父目录推导而不是新增一个配置项：数据目录是可配置的，而
    知识库与其余持久化数据同处一块可写卷——容器部署时才不会出现「卷挂上了、知识库
    却写进了镜像层」这种重启即丢失的问题。

    Raises:
        ValueError: ``config`` 为 ``None``。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    return Path(config.db_path).parent / KNOWLEDGE_DB_NAME


async def ensure_service(config: AppConfig | None = None) -> KnowledgeService:
    """返回进程内唯一的知识库服务，首次调用时装配。

    Args:
        config: 应用配置；首次调用必须提供，已装配时可省略。

    Returns:
        已装配的知识库服务。

    Raises:
        RuntimeError: 尚未装配且调用方未提供 ``config``。
        KnowledgeStoreError: 存量索引的维度或模型与当前配置不符（需重建）。
    """
    global _service, _stack  # noqa: PLW0603 - 见模块 docstring：这是刻意的进程级句柄

    if _service is not None:
        return _service

    async with _lock:
        # 双检：并发首次调用时，等锁期间可能已被另一个协程装配好
        if _service is not None:
            return _service
        if config is None:
            raise RuntimeError("知识库尚未装配：首次调用 ensure_service 必须传入 config")

        stack = AsyncExitStack()
        embeddings = build_embeddings(config)
        try:
            store = await stack.enter_async_context(
                open_knowledge_store(
                    knowledge_db_path(config),
                    dims=config.embedding_dims,
                    model=config.embedding_model,
                    # WHY 用「有没有嵌入后端」决定要不要向量表，而不是另加一个开关：
                    # 没有后端却建出向量表，只会得到一张永远为空的表和一个会失败的
                    # 检索分支；同一个事实只有一个来源。
                    vector_enabled=embeddings is not None,
                )
            )
            if embeddings is not None:
                # WHY 把嵌入后端的关闭也压进同一个退出栈：子进程档位下它是一个常驻
                # 进程（实测 189 MB），漏关就是留一个孤儿。
                stack.push_async_callback(embeddings.aclose)
        except BaseException:
            # 含 CancelledError：装配中途被取消时也要把已开的资源收干净
            await stack.aclose()
            raise

        _stack = stack
        _service = KnowledgeService(config, store=store, embeddings=embeddings)
        logger.info(
            "知识库已装配：db=%s 向量=%s 嵌入=%s",
            knowledge_db_path(config),
            store.vector_enabled,
            embeddings.name if embeddings is not None else "none",
        )
        return _service


def peek_service() -> KnowledgeService | None:
    """已装配时返回服务，否则 ``None``；**不触发装配**。

    WHY 需要它：就绪探测与接口的能力公示要能在「知识库还没装起来」时如实回答，
    而不是顺手把它装起来——那会让一次健康检查产生一次数据库连接。
    """
    return _service


async def close_service() -> None:
    """关闭知识库并清空句柄；可重复调用。

    WHY 幂等：它同时出现在正常退出与异常退出两条路径上，而重复关闭不该再报一次错
    ——那会让真正的退出原因被一条无关的关闭失败盖住。
    """
    global _service, _stack  # noqa: PLW0603 - 与 ensure_service 同一理由

    async with _lock:
        stack, _stack = _stack, None
        _service = None

    if stack is not None:
        await stack.aclose()
        logger.info("知识库已关闭")
