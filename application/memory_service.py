"""长期记忆管理服务。

职责边界：只回答「某个主体记住了什么、能不能删掉某一条」，不参与图运行，
也不决定 Agent 何时写记忆（那是 ``StoreBackend`` 与模型侧的事）。

WHY 需要它：``/memories/`` 此前只能由 Agent 在对话里读写——用户既看不到
「它到底记住了我什么」，也删不掉记错的内容。而记忆会进入后续每一轮上下文，
一条记错的内容等于让 Agent 长期跑偏；「可查看 + 可删除」是让记忆成为可控
资产而不是黑箱的最小闭环。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agent.run_context import ANONYMOUS_USER_ID, memory_namespace
from application.audit_context import audit_client_info, audit_trace_id
from application.dto import MemoryDeleteResult, MemoryItem, MemoryListResult
from application.errors import PermissionDeniedError
from application.ownership import UNAUTHENTICATED_OWNER, effective_owner_id

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

    from application.principal import Principal
    from config import AppConfig
    from runtime.audit_store import AuditStore

logger = logging.getLogger(__name__)

MEMORY_PATH_PREFIX = "/memories"
"""记忆在虚拟文件系统里的挂载点。

WHY 对外统一用挂载点路径：用户与模型看到的都是 ``/memories/notes.md``，接口
若直接返回存储层的裸键（``/notes.md``）会让人对不上号。路径形式的转换只发生
在本模块与存储之间。
"""

_MAX_ITEMS = 200
"""单次清单返回的最大条数。

WHY 需要上限：Store 的 ``search`` 支持分页，但记忆面板不是会话列表——它的
正常规模是个位数到几十条。无上限地全量返回，等于给了「一次写入上千条」
这种异常情况一个拖垮接口的机会。
"""

_MAX_CONTENT_CHARS = 4000
"""单条记忆返回的正文上限。

WHY 截断：记忆本应是短条目，但模型完全可能把整篇文档写进去；一条几百 KB 的
记录会让面板卡死。截断后的结果带 ``truncated`` 标志，界面据此提示用户。
"""

_END_OF_CONTENT = "\n…（已截断）"


def to_store_key(path: str) -> str:
    """把对外路径（``/memories/x.md``）转换成存储层的键（``/x.md``）。

    WHAT：``StoreBackend`` 挂载在 ``/memories/`` 下，且被 ``CompositeBackend``
    剥掉了前缀，因此存储键是「去掉挂载点之后」的相对路径。

    WHY 同时接受 ``/x.md``：存储层的键就是这个形状，管理脚本或按存储口径
    排查时也会这么写；两种写法都收，避免调用方猜。

    Args:
        path: 记忆路径。

    Returns:
        存储层使用的键，形如 ``/notes.md``。

    Raises:
        ValueError: 路径为空、不是字符串、指向目录、含 ``..`` 段或空字节。
    """
    if not isinstance(path, str):
        raise ValueError(f"path 必须是字符串，实际：{type(path).__name__}")

    # WHY 容忍反斜杠：Windows 上从资源管理器复制路径是常态，统一归一后
    # 报错信息才不会变成「路径不存在」这种误导性结论。
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("path 不能为空")
    if "\x00" in normalized:
        raise ValueError("path 不能包含空字节")
    if not normalized.startswith("/"):
        normalized = "/" + normalized

    if normalized == MEMORY_PATH_PREFIX or normalized.startswith(MEMORY_PATH_PREFIX + "/"):
        normalized = normalized[len(MEMORY_PATH_PREFIX) :]

    if not normalized or normalized == "/":
        raise ValueError("path 必须指向具体记忆条目，而不是 /memories/ 目录")
    if normalized.endswith("/"):
        raise ValueError("path 必须指向具体记忆条目，不能以 / 结尾")
    if ".." in normalized.split("/"):
        raise ValueError("path 不能包含 .. 段")

    return normalized


def to_virtual_path(key: str) -> str:
    """把存储层的键还原成对外路径（``/x.md`` → ``/memories/x.md``）。"""
    if not isinstance(key, str) or not key:
        raise ValueError("key 必须是非空字符串")
    if key == MEMORY_PATH_PREFIX or key.startswith(MEMORY_PATH_PREFIX + "/"):
        return key
    return MEMORY_PATH_PREFIX + (key if key.startswith("/") else "/" + key)


def _content_of(value: dict[str, Any], key: str) -> str:
    """从存储值里取出正文。

    WHY 容忍 ``list[str]``：``StoreBackend`` 的旧版本把正文存成行列表，老库里
    可能残留这种形状；读不出来时降级成空串并告警，总比让整个清单接口报错好。
    """
    raw = value.get("content")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list) and all(isinstance(line, str) for line in raw):
        logger.debug("记忆 %s 使用旧版行列表格式，已按行拼接", key)
        return "\n".join(raw)
    if raw is None:
        return ""
    logger.warning("记忆 %s 的正文格式无法识别（%s），按空内容展示", key, type(raw).__name__)
    return ""


class MemoryService:
    """长期记忆的查询与删除。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        store: BaseStore,
        audit_store: AuditStore | None = None,
    ) -> None:
        """构造服务。

        Args:
            config: 应用配置，决定鉴权模式与主体归属口径。
            store: 长期记忆存储；与图共享同一实例，否则面板看到的与 Agent
                写入的不是同一份数据。
            audit_store: 审计存储；``None`` 时不记录删除事件。

        Raises:
            ValueError: 任一必需依赖为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if store is None:
            raise ValueError("store 不能为 None")

        self._config = config
        self._store = store
        self._audit_store = audit_store

        logger.info("记忆服务就绪：auth_mode=%s", config.auth_mode)

    async def list_memories(self, principal: Principal | None = None) -> MemoryListResult:
        """列出当前主体可见的长期记忆。

        Args:
            principal: 当前主体；``None`` 仅在调用方已完成鉴权时使用。

        Returns:
            按路径排序的记忆清单；超出上限时 ``truncated`` 为 ``True``。

        Raises:
            PermissionDeniedError: 已启用鉴权但主体不可识别。
            RuntimeError: 读取存储失败（调用方应映射为 500）。
        """
        owner = self._owner(principal)
        namespace = memory_namespace(owner)
        try:
            # WHY 多取一条：分页接口用「比上限多一条」判断是否还有剩余，
            # 这样不必额外发一次计数查询，也不会因为存了恰好等于上限的
            # 条数而误报截断。
            records = await self._store.asearch(namespace, limit=_MAX_ITEMS + 1)
        except Exception as exc:
            logger.exception("读取长期记忆失败：owner=%s", owner)
            raise RuntimeError("读取长期记忆失败") from exc

        truncated = len(records) > _MAX_ITEMS
        records = sorted(records[:_MAX_ITEMS], key=lambda item: str(item.key))

        items: list[MemoryItem] = []
        for record in records:
            content = _content_of(record.value, str(record.key))
            item_truncated = len(content) > _MAX_CONTENT_CHARS
            value = record.value
            items.append(
                MemoryItem(
                    path=to_virtual_path(str(record.key)),
                    content=content[:_MAX_CONTENT_CHARS] + _END_OF_CONTENT
                    if item_truncated
                    else content,
                    created_at=str(value.get("created_at") or ""),
                    updated_at=str(value.get("modified_at") or value.get("updated_at") or ""),
                    truncated=item_truncated,
                )
            )
            truncated = truncated or item_truncated

        logger.debug("长期记忆清单完成：owner=%s 返回 %d 条", owner, len(items))
        return MemoryListResult(owner_id=owner, items=items, total=len(items), truncated=truncated)

    async def delete_memory(
        self,
        path: str,
        principal: Principal | None = None,
    ) -> MemoryDeleteResult:
        """删除一条长期记忆。

        WHY 删除不存在的条目不算失败：这是一条幂等的「忘掉它」请求，界面在
        并发刷新后可能重复提交；把它变成 404 只会让用户看到一条与真实结果
        无关的报错。``deleted=False`` 保留了「本次确实没删到」这一信息。

        Args:
            path: 记忆路径，``/memories/x.md`` 与 ``/x.md`` 均可。
            principal: 当前主体；``None`` 仅在调用方已完成鉴权时使用。

        Returns:
            删除结果；``deleted`` 为 ``False`` 表示该路径本就不存在。

        Raises:
            ValueError: 路径非法（调用方应映射为 400）。
            PermissionDeniedError: 已启用鉴权但主体不可识别。
            RuntimeError: 存储操作失败（调用方应映射为 500）。
        """
        key = to_store_key(path)
        owner = self._owner(principal)
        namespace = memory_namespace(owner)
        virtual = to_virtual_path(key)

        try:
            existing = await self._store.aget(namespace, key)
        except Exception as exc:
            logger.exception("读取待删除记忆失败：owner=%s path=%s", owner, virtual)
            raise RuntimeError("删除长期记忆失败") from exc

        if existing is None:
            logger.info("删除长期记忆：条目不存在，视为已满足 owner=%s path=%s", owner, virtual)
            return MemoryDeleteResult(path=virtual, deleted=False)

        try:
            await self._store.adelete(namespace, key)
        except Exception as exc:
            logger.exception("删除长期记忆失败：owner=%s path=%s", owner, virtual)
            raise RuntimeError("删除长期记忆失败") from exc

        logger.info("已删除长期记忆：owner=%s path=%s", owner, virtual)
        await self._audit(path=virtual, owner=owner, principal=principal)
        return MemoryDeleteResult(path=virtual, deleted=True)

    # ------------------------------------------------------------------ 内部

    def _owner(self, principal: Principal | None) -> str:
        """返回本次操作归属的主体标识。

        WHY 鉴权开启而主体不可识别时直接拒绝：记忆是主体私有数据，此时返回
        空清单会把「你是谁」这个问题伪装成「你什么都没记住」，属于静默失败。
        """
        owner = effective_owner_id(self._config, principal)
        if owner is None:
            # 认证关闭：本地单用户，与会话归属同一口径（``RunHandle.memory_owner``）。
            return ANONYMOUS_USER_ID
        if owner == UNAUTHENTICATED_OWNER:
            raise PermissionDeniedError("memory:read")
        return owner

    async def _audit(
        self,
        *,
        path: str,
        owner: str,
        principal: Principal | None,
    ) -> None:
        """记录一次记忆删除事件。

        WHY 只记录「确实删掉了」的动作：删除是破坏性操作，需要留痕；而
        「删一条不存在的记忆」没有改变任何状态，记进审计只会稀释真正的事件。

        WHY 审计失败不上抛：审计是旁路职责，把一次成功的删除变成 500 会让
        「审计库满」演变成「记忆删不掉」。
        """
        if self._audit_store is None:
            return
        actor_id = principal.user_id if principal is not None else ANONYMOUS_USER_ID
        ip, ua = audit_client_info()
        try:
            await self._audit_store.log(
                event_type="memory_delete",
                actor_id=actor_id,
                target_id=path,
                action="delete",
                outcome="success",
                ip=ip,
                user_agent=ua,
                # WHY 与 IP/UA 同一处读取：删除是破坏性动作，它属于哪次请求是留痕的
                # 关键一半——只有「谁删的」「何时删的」而没有「哪次操作删的」，
                # 一次批量删除会散成一堆互不相干的记录。
                trace_id=audit_trace_id(),
                details={"owner_id": owner},
            )
        except Exception:
            logger.exception("审计事件写入失败：event_type=memory_delete path=%s", path)

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志排错
        return f"MemoryService(auth_mode={self._config.auth_mode})"


__all__ = ["MEMORY_PATH_PREFIX", "MemoryService", "to_store_key", "to_virtual_path"]
