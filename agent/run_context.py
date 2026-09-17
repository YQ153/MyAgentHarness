"""图运行时上下文与长期记忆命名空间。

WHY 需要把「这一轮是谁在跑」带进图里：``/memories/`` 由 ``StoreBackend``
承载，而它的命名空间是在**调用时**由节点/工具计算的——拿不到主体标识，
记忆就只能落进一个全局池子：开了鉴权之后，B 用户的 Agent 能读到 A 用户
写下的内容，这不是「记忆没隔离」这种体验问题，而是跨用户数据泄漏。

可选的传递方式只有两条：LangGraph 的 ``Runtime.context``（显式、可在装配期
声明类型）与 ``ContextVar``（隐式环境态）。这里选前者——ContextVar 会把
「谁在跑」变成一段看不见的全局状态，任何绕过服务层直接调图的代码都会静默
落进别人的记忆池，而且这类错误在测试里几乎不可能被发现。

本模块只依赖标准库，供 ``agent.backends``（读命名空间）、``agent.graph``
（声明 context_schema）与 ``application``（构造上下文、管理接口）共用。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

ANONYMOUS_USER_ID = "__anonymous__"
"""未启用鉴权时的记忆归属标识。

WHY 要有兜底主体而不是用空串：``StoreBackend`` 也可以在图外被调用（管理
接口直接读写、测试直接驱动 backend），此时拿不到运行时；若用空串，命名空间
组件会被 deepagents 判为非法并抛错，表现为「记忆功能整体不可用」。统一落到
本标识后，本地单用户场景仍是同一个记忆池，语义与"一个人一台机器"一致。

``application.principal.ANONYMOUS_PRINCIPAL`` 复用本常量，避免两处各写一份
字面量后悄悄漂移成两个池子。
"""

MEMORY_NAMESPACE_ROOT = "memories"
"""长期记忆命名空间的根组件。

WHY 固定根前缀：Store 是共享的键值空间，日后还会有别类数据；有根前缀才能
按 ``list_namespaces`` 枚举与归类，也避免与未来某个业务的前缀撞名。
"""

_NAMESPACE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9\-_.@+:~]+$")
"""命名空间组件允许的字符集。

与 deepagents ``StoreBackend._validate_namespace`` 的规则保持一致：它会在
每次读写时校验命名空间，含非法字符则抛 ``ValueError``——而那条路径是工具
调用，报错表现为「Agent 写不了记忆」，与真实原因（某个 OIDC ``sub`` 里有个
``|``）相距甚远。对齐方式见 ``tests/agent/test_memory_namespace.py``：用例
直接拿 deepagents 的校验函数验证本模块的输出。
"""


@dataclass(frozen=True, slots=True)
class AgentRunContext:
    """一次图运行的上下文。

    WHY 用 dataclass 而不是 pydantic 模型：它只在进程内传递，不需要序列化
    与字段级校验器；``frozen`` 保证节点拿到的归属不会被中途改写（同一轮
    运行里出现两个主体，记忆与审计会对不上）。

    Attributes:
        user_id: 本轮运行的主体标识，决定长期记忆的命名空间。
    """

    user_id: str = ANONYMOUS_USER_ID

    def __post_init__(self) -> None:
        """校验并归一主体标识。

        Raises:
            ValueError: ``user_id`` 不是非空字符串。
        """
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError(
                f"user_id 必须是非空字符串，实际：{type(self.user_id).__name__}"
            )
        if self.user_id != self.user_id.strip():
            object.__setattr__(self, "user_id", self.user_id.strip())


@lru_cache(maxsize=1024)
def memory_namespace(user_id: str | None) -> tuple[str, str]:
    """返回某主体的长期记忆命名空间。

    WHY 缓存：本函数在每一次 ``/memories/`` 文件操作（ls / read / write /
    edit）里都会被调用一次，而结果只取决于主体标识。缓存同时让「标识含非法
    字符」的告警每个主体只打一次，而不是每次工具调用都刷屏。

    Args:
        user_id: 主体标识；``None`` / 空串表示未认证。

    Returns:
        ``(根前缀, 主体组件)`` 二元组，可直接交给 ``StoreBackend``。
    """
    return (MEMORY_NAMESPACE_ROOT, _namespace_component(user_id))


def memory_owner_of(runtime: Any) -> str:
    """从图运行时里取出记忆归属主体。

    WHY 全程用 ``getattr`` 兜底：``StoreBackend`` 在图外被调用时，deepagents
    找不到运行时会把 ``None`` 交给命名空间工厂；此时按匿名主体归档，比让
    每一次文件操作都抛异常更符合「本地单用户」的语义。

    Args:
        runtime: LangGraph 传入的 ``Runtime``；图外调用时为 ``None``。

    Returns:
        主体标识；取不到时返回 :data:`ANONYMOUS_USER_ID`。
    """
    user_id = getattr(getattr(runtime, "context", None), "user_id", None)
    if isinstance(user_id, str) and user_id.strip():
        return user_id
    logger.debug("运行时未携带主体标识，长期记忆按匿名主体归档")
    return ANONYMOUS_USER_ID


def namespace_of_runtime(runtime: Any) -> tuple[str, str]:
    """``StoreBackend`` 的命名空间工厂：运行时 → 命名空间。"""
    return memory_namespace(memory_owner_of(runtime))


def _namespace_component(user_id: str | None) -> str:
    """把主体标识转换成合法的命名空间组件。

    WHAT：字符集合法时原样返回（保持可读、可人工核对），否则退化为
    ``sha256`` 前 32 位十六进制。

    WHY 需要转换而不是直接报错：主体标识来自 IdP，常见的 ``auth0|abc``、
    含 ``/`` 的 URL 型 ``sub`` 都含非法字符；直接报错等于「这类 IdP 的用户
    用不了记忆」，而截断或替换前缀又会把不同的标识压成同一个组件；散列是
    唯一既稳定（同一标识恒等映射）又几乎不冲突的选择。

    WHY 要打告警：散列后无法从命名空间反推主体，排障时若不知道发生过转换，
    会误以为「记忆为空」是功能故障。

    Args:
        user_id: 主体标识；``None`` / 空白串按匿名主体处理。

    Returns:
        合法的命名空间组件。
    """
    if not isinstance(user_id, str) or not user_id.strip():
        return ANONYMOUS_USER_ID

    cleaned = user_id.strip()
    if _NAMESPACE_COMPONENT_RE.match(cleaned):
        return cleaned

    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:32]
    logger.warning(
        "主体标识含记忆命名空间不允许的字符，已改用散列 %s（记忆仍可用，"
        "但无法从命名空间反推主体）",
        digest,
    )
    return digest


__all__ = [
    "ANONYMOUS_USER_ID",
    "MEMORY_NAMESPACE_ROOT",
    "AgentRunContext",
    "memory_namespace",
    "memory_owner_of",
    "namespace_of_runtime",
]
