"""长期记忆存储。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langgraph.store.memory import InMemoryStore

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)


def build_store(config: AppConfig) -> InMemoryStore:
    """构造应用级 Store。

    WHY 使用内存 Store 而非持久化实现：``/memories/`` 承载的是跨会话的用户
    偏好与项目约定，体量极小；引入外部存储会带来新的部署依赖与并发语义，
    收益不抵成本。若后续需要多实例共享，替换此处实现即可，
    ``StoreBackend`` 只依赖 ``BaseStore`` 抽象。
    """
    if config is None:
        raise ValueError("config 不能为 None")

    logger.info("长期记忆存储已初始化（进程内）")
    return InMemoryStore()
