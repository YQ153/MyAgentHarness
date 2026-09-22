"""知识库的进程级句柄与惰性装配（**按工作区各一份**）。

WHY 需要一个进程级句柄，而不是在每处各自装配：

- 自定义工具扩展点（``CUSTOM_TOOL_MODULES``）只把 ``config`` 交给工具模块、**没有**
  注入依赖的通道；而知识库持有的是一条 SQLite 连接（打开是异步的、生命周期应与
  进程一致），不可能每次工具调用都开一份。
- 于是装配集中在这里：``bootstrap`` 在启动时建起来并负责关闭，工具与接口取的是
  同一批实例——与「图与记忆面板共享同一个 store」同一个理由，各持一份会让两边
  看到不同的事实。

WHY 句柄按**工作区**分开（``dict[绝对路径] -> (退出栈, 服务)``）：索引的对象是工作区
里的文档，而文档在库里以**工作区内的虚拟路径**为键去重（``UNIQUE(owner_id,
source_path)``）。两个工作区里的 ``/src/index.ts`` 会因此撞成同一行——索引互相覆盖，
「删除某份文档」还会删到另一个项目的同名文件。每个工作区一份独立库是唯一既彻底、
又不必改动全部检索查询口径的隔离方式。

WHY 嵌入后端仍然**全局共享一份**：子进程档位下它是一个常驻进程（实测 189 MB），
按工作区各拉一个会让「换一个工作区」变成一次几百毫秒的进程启动，工作区一多就把内存
吃光。嵌入与工作区无关（同一模型、同一维度），共享不产生串味。

WHY 不做成导入期就构造的模块级单例：它需要 ``await``；而且构造失败应当落在**启动
路径**上（能被看见、能拦住进程），而不是发生在某个模块被 import 的那一刻。

数据库落在**工作区内**的 ``<工作区>/.harness/knowledge.db``（2026-09-22 改），与检查点等
库**分开**：

- 索引的对象是工作区里的文档（库里以根内虚拟路径为键去重），跟着项目走才能让「删项目 =
  删索引」「备份项目带上索引」同时成立；放在数据目录下会出现「项目删了、索引还在」；
- 向量维度或模型一变就必须整库重建，独立文件让「删掉重来」是一条明确可执行的指令；
- ``vec0`` 是加载式扩展，把它写进主库会让「扩展在当前环境不可用」与「检查点库」
  纠缠在一起——那两件事的处置方式完全不同。

升级路径：旧库在 ``<数据目录>/knowledge-<根标识>.db``，首次装配某个根时由
``_migrate_legacy_db`` 搬进该根（``legacy_knowledge_db_path`` 保留旧公式用于定位）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from application.knowledge_service import KnowledgeService
from config import SessionRoot
from llm.embeddings import build_embeddings
from runtime.knowledge_store import open_knowledge_store

if TYPE_CHECKING:
    from config import AppConfig
    from llm.embeddings import EmbeddingBackend

logger = logging.getLogger(__name__)

KNOWLEDGE_DB_PREFIX = "knowledge-"
"""**旧版**知识库文件名前缀（完整形态是前缀 + 文件根标识 + ``.db``）。

WHY 仍然保留：升级路径上要靠它与 :func:`workspace_slug` 定位旧库（见
``legacy_knowledge_db_path``），排障脚本 ``scripts/inspect_thread.py`` 也会用它去数据目录
里找历史库。新代码不应再按这个前缀拼库名——现行路径由 ``SessionRoot.knowledge_db`` 给出，
库里以「根内的虚拟路径」为键去重（两个项目的 ``/README.md`` 是同一个键）。
"""

_SLUG_ALLOWED = re.compile(r"[^A-Za-z0-9_.-]+")
"""目录名里可以原样保留的字符；其余一律换成 ``_``，只为让文件名可读。"""

_SLUG_MAX_CHARS = 40
"""文件名里保留的原目录名长度上限。"""

_services: dict[str, KnowledgeService] = {}
"""已装配的工作区标识 → 知识库服务。"""

_stacks: dict[str, AsyncExitStack] = {}
"""已装配的工作区标识 → 该库的退出栈（只负责这一条连接）。"""

_embeddings: EmbeddingBackend | None = None
_embeddings_stack: AsyncExitStack | None = None
_embeddings_built = False
"""嵌入后端与其是否**已经尝试过**构造。

WHY 要一个独立的「试过了」标志：构造结果本身可能是 ``None``（表示不启用嵌入），
仅凭 ``_embeddings is None`` 分不清「还没建」与「建过、结论是不启用」，于是每次
取用都会重新构造一遍。
"""

_lock = asyncio.Lock()


def workspace_slug(workspace: Path) -> str:
    """把工作区路径换算成一个稳定、可读的库文件名标识。

    WHY 用「目录名 + 路径散列」而不是只用目录名：不同父目录下同名的 ``app`` 会有
    相同 slug，两个工作区就会共用一个库——而那正是本次要消除的串味。散列取自**完整
    绝对路径**，碰撞概率可忽略；保留目录名只为让人看着文件名能猜到是哪个项目。

    WHY 不把整条路径塞进文件名：Windows 路径含 ``:`` 与 ``\\``，不能直接进文件名；
    把它们替换成下划线又会让 ``C:\\a\\b`` 与 ``C_a_b`` 撞名。

    Raises:
        ValueError: ``workspace`` 为空。
    """
    if workspace is None:
        raise ValueError("workspace 不能为 None")
    resolved = Path(workspace).expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    leaf = _SLUG_ALLOWED.sub("_", resolved.name)[:_SLUG_MAX_CHARS].strip("_.-") or "workspace"
    return f"{leaf}-{digest}"


def knowledge_db_path(config: AppConfig, workspace: Path) -> Path:
    """返回某个文件根的知识库文件路径（``<工作区>/.harness/knowledge.db``）。

    WHY 落在工作区内（2026-09-22 改）：索引的对象就是工作区里的文档，库里以「根内虚拟
    路径」为键去重（``UNIQUE(owner_id, source_path)``）。放在项目里，「删项目 = 删索引」与
    「备份项目就带上索引」同时成立；放在数据目录下则会出现「项目删了、索引还在」，而那份
    索引永远指不回去。

    WHY 仍是一个根一个文件、没有例外：两个项目的 ``/README.md`` 是同一个键——共用一个库
    就会互相覆盖索引，「删除这份文档」还会删到另一个项目的同名文件。

    WHY 路径由 :class:`SessionRoot` 给出、而不在这里拼 ``.harness``：目录布局只有一处
    出处，本模块只消费它的结论。

    Args:
        config: 应用配置。
        workspace: 目标文件根（用户工作空间或会话专属目录）。

    Raises:
        ValueError: ``config`` 或 ``workspace`` 为 ``None``。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    if workspace is None:
        raise ValueError("workspace 不能为 None：知识库按文件根隔离，缺了它就不知道该开哪个库")
    return SessionRoot(config, Path(workspace).expanduser().resolve()).knowledge_db


def legacy_knowledge_db_path(config: AppConfig, workspace: Path) -> Path:
    """**旧布局**（2026-09-22 之前）的知识库文件路径：``<数据目录>/knowledge-<根标识>.db``。

    WHY 还需要这个公式：升级时要把旧库搬进工作区，而定位它必须与旧版本逐字一致——差一个
    字符就会得到「没有旧库」，而表现是用户的索引凭空消失（需要重新嵌入一遍）。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    if workspace is None:
        raise ValueError("workspace 不能为 None")
    resolved = Path(workspace).expanduser().resolve()
    return Path(config.db_path).parent / f"{KNOWLEDGE_DB_PREFIX}{workspace_slug(resolved)}.db"


def _migrate_legacy_db(config: AppConfig, workspace: Path, target: Path) -> None:
    """把旧布局的知识库搬到新位置；幂等，且**不覆盖**已存在的新库。

    WHY 迁移而不是重建：重建等于跑一次全量嵌入（分钟级、且要求嵌入后端可用），而用户并不
    知道「升级会让我重新索引」。搬过去不丢任何东西，代价也只是一次文件 move。

    WHY 新库已存在时不覆盖：两份库可能对应不同的维度或嵌入模型，静默覆盖会让「升级后检索
    结果变了」成为一个没有线索的现象。两边都在时只告警，由用户决定删哪一份。
    """
    if target.exists():
        return
    legacy = legacy_knowledge_db_path(config, workspace)
    if not legacy.is_file():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
    except OSError as exc:
        # 搬不动就停在原处并如实报错：静默继续会以「新位置有个空库」的形式收场，用户只会
        # 看到「索引空了」，而日志里必须留下原因与手工修复的路径。
        logger.error(
            "旧知识库迁移失败，索引可能需要重建（请手工搬到 %s）：%s（%s）",
            target,
            legacy,
            exc,
        )
        return
    logger.info("旧知识库已搬进工作区：%s → %s", legacy, target)


def _workspace_key(workspace: Path) -> str:
    """把工作区路径归一成句柄字典的键。

    Raises:
        ValueError: ``workspace`` 为空。
    """
    if workspace is None:
        raise ValueError("workspace 不能为 None")
    return str(Path(workspace).expanduser().resolve())


async def _ensure_embeddings(config: AppConfig) -> EmbeddingBackend | None:
    """取（必要时构造）进程内共享的嵌入后端。

    WHY 共享：见模块 docstring——嵌入与工作区无关，按工作区各拉一份既慢又费内存。

    Raises:
        EmbeddingError: 后端构造失败。向上抛出而不降级：配错了就该在启动时暴露，
            而不是让每个工作区各自静默退化成关键词检索。
    """
    global _embeddings, _embeddings_built, _embeddings_stack  # noqa: PLW0603 - 进程级句柄

    if _embeddings_built:
        return _embeddings

    stack = AsyncExitStack()
    try:
        backend = build_embeddings(config)
        if backend is not None:
            # WHY 把嵌入后端的关闭压进**共享**退出栈：它跨工作区复用，跟着某个工作区
            # 的库一起关掉，会让其余工作区的检索静默失效。
            stack.push_async_callback(backend.aclose)
    except BaseException:
        # 含 CancelledError：构造中途被取消时也要把已开的资源收干净
        await stack.aclose()
        raise

    _embeddings_stack = stack
    _embeddings = backend
    _embeddings_built = True
    return backend


async def ensure_service(
    config: AppConfig | None = None,
    workspace: Path | None = None,
    *,
    scope: SessionRoot | None = None,
) -> KnowledgeService:
    """返回某个文件根的知识库服务，首次为它调用时装配。

    Args:
        config: 应用配置；首次为某个根调用时必须提供。
        workspace: 目标文件根；与 ``scope`` 二选一。
        scope: 目标会话根；给了它就以 ``scope.root`` 为准。

    Returns:
        该根已装配的知识库服务。

    Raises:
        ValueError: 既没给 ``workspace`` 也没给 ``scope``（没有根就不知道该开哪个库）。
        RuntimeError: 该根尚未装配，且调用方未提供 ``config``。
        KnowledgeStoreError: 存量索引的维度或模型与当前配置不符（需重建）。
    """
    if scope is not None:
        target = scope.root
    else:
        target = workspace
    if target is None:
        # WHY 不能悄悄退到某个「默认目录」：那会让两个本来无关的项目共用一份索引，
        # 而库里以「根内虚拟路径」为键去重——两个项目的 /README.md 是同一个键。
        raise ValueError(
            "知识库按文件根隔离，必须给一个根：请传 workspace 或 scope"
            "（首条消息之前这条会话还没有根）"
        )

    key = _workspace_key(target)
    cached = _services.get(key)
    if cached is not None:
        return cached

    async with _lock:
        # 双检：并发首次调用时，等锁期间可能已被另一个协程装配好
        cached = _services.get(key)
        if cached is not None:
            return cached
        if config is None:
            raise RuntimeError(
                f"知识库尚未为文件根 {key} 装配：首次调用 ensure_service 必须传入 config"
            )
        return await _assemble(config, key, scope)


async def _assemble(config: AppConfig, key: str, scope: SessionRoot | None) -> KnowledgeService:
    """真正装配一个根的知识库（调用方必须已持有 ``_lock``）。"""
    embeddings = await _ensure_embeddings(config)
    db_path = knowledge_db_path(config, Path(key))
    # WHY 必须在开库之前迁移：一旦按新路径打开，创建逻辑会把「新位置没有库」当成全新库建表，
    # 于是旧库还躺在数据目录里、用户看到的却是「索引空了」——没有任何报错指向真正的原因。
    _migrate_legacy_db(config, Path(key), db_path)
    stack = AsyncExitStack()
    try:
        store = await stack.enter_async_context(
            open_knowledge_store(
                db_path,
                dims=config.embedding_dims,
                model=config.embedding_model,
                # WHY 用「有没有嵌入后端」决定要不要向量表，而不是另加一个开关：
                # 没有后端却建出向量表，只会得到一张永远为空的表和一个会失败的
                # 检索分支；同一个事实只有一个来源。
                vector_enabled=embeddings is not None,
            )
        )
    except BaseException:
        # 含 CancelledError：装配中途被取消时也要把已开的资源收干净
        await stack.aclose()
        raise

    _stacks[key] = stack
    service = KnowledgeService(
        config,
        scope=scope if scope is not None else SessionRoot(config, Path(key)),
        store=store,
        embeddings=embeddings,
    )
    _services[key] = service
    logger.info(
        "知识库已装配：root=%s db=%s 向量=%s 嵌入=%s",
        key,
        db_path,
        store.vector_enabled,
        embeddings.name if embeddings is not None else "none",
    )
    return service


def peek_service(workspace: Path | None = None) -> KnowledgeService | None:
    """已装配时返回服务，否则 ``None``；**不触发装配**。

    WHY 需要它：就绪探测与接口的能力公示要能在「知识库还没装起来」时如实回答，
    而不是顺手把它装起来——那会让一次健康检查产生一次数据库连接。

    WHY 允许 ``workspace=None``：调用方（如全局健康检查）常常只想知道「有没有任何
    一个工作区装了知识库」，此时按「任意一个」回答比强迫它先解析工作区更合适。
    """
    if workspace is None:
        return next(iter(_services.values()), None)
    return _services.get(_workspace_key(workspace))


async def close_service() -> None:
    """关闭全部工作区的知识库与共享嵌入后端；可重复调用。

    WHY 幂等：它同时出现在正常退出与异常退出两条路径上，而重复关闭不该再报一次错
    ——那会让真正的退出原因被一条无关的关闭失败盖住。

    WHY 先关各工作区的库、再关共享嵌入后端：反过来会让正在关闭的库对着一台已经
    停掉的嵌入服务做收尾工作。
    """
    global _embeddings, _embeddings_built, _embeddings_stack  # noqa: PLW0603 - 进程级句柄

    async with _lock:
        stacks = list(_stacks.values())
        _stacks.clear()
        _services.clear()
        shared = _embeddings_stack
        _embeddings_stack = None
        _embeddings = None
        _embeddings_built = False

    # WHY 不在锁内 await 关闭：关闭要等 I/O，握着锁会把并发的装配请求一起堵住，
    # 而此刻句柄已经清空，新来的请求会重新装配（而不是读到半关的旧实例）。
    for stack in stacks:
        await stack.aclose()
    if shared is not None:
        await shared.aclose()
    if stacks or shared is not None:
        logger.info("知识库已关闭：文件根 %d 个", len(stacks))


__all__ = [
    "KNOWLEDGE_DB_PREFIX",
    "close_service",
    "ensure_service",
    "knowledge_db_path",
    "legacy_knowledge_db_path",
    "peek_service",
    "workspace_slug",
]
