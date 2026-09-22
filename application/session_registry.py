"""会话的文件根：解析、装配，以及挑选目录的两种方式。

## 模型（两条规则）

1. **根只有两种来源**：用户在建会话时选定的**工作空间**（可同时容纳多条会话），或者
   应用为「没选工作空间」的会话在 ``SESSIONS_ROOT`` 下自动创建的**会话专属目录**。
   两者在解析之后完全等价——文件工具、沙箱挂载根、附件目录与知识库索引需要的都只是
   一个根。
2. **根一旦确定、且这条会话产生过第一条交互，就永久锁定**。锁定不写在「创建会话」那一刻：
   那时用户还在选；写在「第一轮交互」那一刻，因为那时 Agent 已经可能读过或写过文件，
   再换根就会让此前写下的产物、附件与索引失联——而那种失联不会报错。
3. **解析返回的根一定可用**（目录存在）。这不是顺手做的事，而是这一层的契约：下游
   （Agent 装配、Backend、附件索引、文件面板）全都假定根存在，而**会话专属目录在第一次
   用到它之前并不存在**——少了这一步，「打开一条还没发过消息的会话」就会在构造 Backend
   时炸出一个 500（那是纯内部断言，用户完全看不懂）。用户选定的目录则由
   ``resolve_user_path`` 保证存在，解析时若不在了就如实报错，见
   ``SessionRootUnavailableError``。

## WHY 需要这一层（而不是各调用点自己拼路径）

- **解析是每次请求都要做的事，且必须能失败**（会话不存在 / 根还没确定 / 已锁定）；
  装配是每个根只做一次的重活。混在一起会让「同一个根被并行装配两次」成为常规路径
  （技能物化视图会被反复重建、SQLite 连接会各开一份）。
- **同一个根只装配一份**：文件面板、附件、技能视图与知识库四类服务都以同一个根为准；
  分开取用迟早拼出「面板指向 A、附件写到 B」这种组合，而那不会报错。
"""

from __future__ import annotations

import asyncio
import logging
import os
import string
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from application.attachment_service import AttachmentService
from application.dto import DirectoryEntry, DirectoryListing, WorkspaceInfo
from application.errors import (
    NotFoundError,
    SessionPresetLockedError,
    SessionRootLockedError,
    SessionRootNotReadyError,
    SessionRootUnavailableError,
)
from application.skill_service import SkillService
from application.workspace_service import WorkspaceService
from runtime.folder_picker import (
    FolderPickerBusyError,
    FolderPickerTimeoutError,
    FolderPickerUnavailableError,
    pick_folder,
)

# WHY 把弹窗的几个异常在这里再导出一次：``interfaces`` 按分层契约不能直接依赖
# ``runtime``（那里是进程管理），而路由要把这几种失败翻译成不同的状态码。
# 经应用层转发，既守住契约，又只有一份定义。
__all__ = [
    "FolderPickerBusyError",
    "FolderPickerTimeoutError",
    "FolderPickerUnavailableError",
    "SessionRegistry",
    "SessionServices",
    "choose_workspace_folder",
    "list_directories",
    "resolve_user_path",
]

if TYPE_CHECKING:
    from application.knowledge_service import KnowledgeService
    from application.ports import AuditLog, SkillState, ThreadMetadataReader
    from config import AppConfig, SessionRoot
    from llm.registry import ModelRegistry

logger = logging.getLogger(__name__)

KnowledgeProvider = Callable[["SessionRoot"], Awaitable["KnowledgeService"]]
"""按根装配知识库服务的回调。

WHY 用回调而不是直接 import ``knowledge_runtime``：那个模块是**根级模块**，它反过来
导入 ``application.knowledge_service``——应用层再导入它就把「根模块 → 应用层 → 根模块」
接成一个环，而且会让应用层依赖一个并不属于它的装配入口。回调由 ``bootstrap`` 注入。
"""


@dataclass(frozen=True)
class SessionServices:
    """一个根里的全部会话级服务。

    WHY 打包成一个对象：这四者必须同源——它们都以同一个 ``root`` 为根，分开取用迟早会
    出现「文件面板指向 A、附件写到 B」这种组合，而那不会报错，只会让产物出现在用户
    没在看的地方。
    """

    root: SessionRoot
    files: WorkspaceService
    attachments: AttachmentService
    skills: SkillService
    knowledge: KnowledgeService


def resolve_user_path(value: str | Path) -> Path:
    """把用户给出的工作空间取值解析成绝对路径，并确认它真的存在且是目录。

    WHY 单独成一个函数、且是**唯一**的校验点：目录不存在时若放行，``ensure_directories``
    会 ``mkdir(parents=True)`` 把它「成功」地变成一个空目录——用户要到第一次让 Agent
    找文件时才发现自己指错了地方，而那时已经很难把症状与拼写联系起来。会话专属目录
    走的是另一条路（按需创建），两条路径的差别恰好就在这一处判定上。

    Args:
        value: 用户给出的路径（可含 ``~``）。

    Returns:
        已解析的绝对路径。

    Raises:
        ValueError: 路径不存在、不是目录，或取值不是路径字符串。
    """
    if not isinstance(value, (str, Path)):
        raise ValueError(f"工作空间必须是路径字符串，实际：{type(value).__name__}")
    if isinstance(value, str) and not value.strip():
        raise ValueError("工作空间不能是空白字符串")
    resolved = Path(value).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"工作空间不存在或不是目录：{resolved}")
    return resolved


def _filesystem_anchors() -> list[Path]:
    """返回「整台机器」这一层的起点：Windows 是各盘符根，POSIX 只有 ``/``。

    WHY 需要它：挑目录总得有个起点，而 Windows 上没有「全局根」，起点只能是盘符列表。
    只列存在的盘符——把不存在的 A: 也列出来，用户点进去只会得到一个空列表，与「这个盘
    是空的」难以区分。
    """
    if os.name != "nt":
        return [Path("/")]
    drives: list[Path] = []
    for letter in string.ascii_uppercase:
        candidate = Path(f"{letter}:/")
        try:
            if candidate.exists():
                drives.append(candidate)
        except OSError:
            continue
    return drives


def list_directories(path: str | None = None) -> DirectoryListing:
    """列出一个目录下的子目录，供界面挑选工作空间。

    WHY 没有边界：工作空间允许用户任意选择（这是产品规则），因此服务端不再过滤位置。
    唯一的门槛是权限（``file:read``）——能选任意目录，就意味着能读任意目录。

    WHY 只列目录、一次只列一层：这一步的用途是挑目录，列文件只会让用户在几十个文件里
    找目标；一次列全整棵树会让响应变成一次全盘扫描。

    Args:
        path: 要列的目录；``None`` / 空白表示**起点页**（盘符或 ``/``）。

    Returns:
        当前目录、可返回的上级与该目录下的子目录。

    Raises:
        ValueError: ``path`` 不存在或不是目录。
    """
    if path is None or not str(path).strip():
        anchors = _filesystem_anchors()
        if len(anchors) == 1:
            return _list_subdirectories(anchors[0])
        return DirectoryListing(
            path="",
            parent=None,
            entries=[
                DirectoryEntry(name=item.name or str(item), path=str(item)) for item in anchors
            ],
        )

    current = Path(str(path)).expanduser().resolve()
    if not current.is_dir():
        # WHY 单独判存在性：列一个不存在的目录会得到「这里没有子目录」，而用户可能是
        # 手输了一个路径——那句提示会让他以为「目录是空的」而不是「路径写错了」。
        raise ValueError(f"目录不存在或不是目录：{current}")
    return _list_subdirectories(current)


def _list_subdirectories(current: Path) -> DirectoryListing:
    """列出一个目录下的子目录。

    WHY 不把不可读的目录当成错误：权限不足（Windows 的系统目录就是如此）不该让整次
    列举失败，列成空目录即可——用户会去别处，而不是收到一个看不懂的错误。
    """
    entries: list[DirectoryEntry] = []
    try:
        children = sorted(current.iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        logger.warning("目录不可读，按空目录返回：%s（%s）", current, type(exc).__name__)
        children = []
    for child in children:
        try:
            if not child.is_dir():
                continue
            resolved = child.resolve()
        except OSError:
            continue
        entries.append(DirectoryEntry(name=child.name, path=str(resolved)))

    # 「上一级」的终止：到文件系统根时 ``parent`` 是它自己，直接回传会让按钮原地不动
    # ——看起来有效、点下去什么也不发生。
    parent_path = current.parent
    parent: str | None = None if parent_path == current else str(parent_path)
    return DirectoryListing(path=str(current), parent=parent, entries=entries)


def choose_workspace_folder(
    config: AppConfig,
    initial: str | None = None,
    *,
    picker: Callable[..., Path | None] | None = None,
) -> str | None:
    """在**服务端**弹出系统文件夹选择对话框，返回选中的目录。

    WHY 这件事要经过应用层：``interfaces`` 按分层契约不能直接依赖 ``runtime``
    （那里才是进程管理）；而「选出来的目录能不能用」这条判定属于应用层。

    Args:
        config: 应用配置（提供起始目录的回落位置）。
        initial: 客户端当前的取值，用作对话框的起始目录。
        picker: 弹窗实现；``None`` 表示用 ``runtime.folder_picker``。测试注入替身，
            否则会在跑测试的机器上弹出一个没人关的窗口。

    Returns:
        选中的绝对路径；用户取消时返回 ``None``。

    Raises:
        ValueError: 选中的路径不存在或不是目录（理论上弹窗只会返回已存在的目录，
            这里只是不信任外部输入）。
        FolderPickerUnavailableError: 环境不支持（无桌面 / 缺 tkinter）。
        FolderPickerBusyError: 已经有一个弹窗在等待操作。
        FolderPickerTimeoutError: 超时未关闭。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    do_pick = picker if picker is not None else pick_folder

    start: Path | None = None
    if initial and str(initial).strip():
        candidate = Path(str(initial)).expanduser()
        if candidate.is_dir():
            start = candidate
    if start is None:
        # 回落顺序：用户主目录 → 会话目录的父目录。用主目录是因为「挑一个项目」绝大多数
        # 情况下就从那里开始；退而求其次是会话目录，它至少保证对话框能打开。
        home = Path.home()
        start = home if home.is_dir() else config.resolved_sessions_root

    chosen = do_pick(initial_dir=start)
    if chosen is None:
        return None
    return str(resolve_user_path(chosen))


class SessionRegistry:
    """会话根的解析与装配。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        thread_store: ThreadMetadataReader,
        skill_store: SkillState,
        model_registry: ModelRegistry,
        knowledge_provider: KnowledgeProvider,
        audit_store: AuditLog | None = None,
    ) -> None:
        """构造注册表。

        Args:
            config: 应用配置，提供会话目录与会话级上限。
            thread_store: 会话元数据读取口；会话的文件根由它给出。
            skill_store: 技能启停状态；技能集是**全应用共享**的一份，不随根分叉。
            model_registry: 模型注册表；附件服务靠它判断多模态能力。
            knowledge_provider: 按根装配知识库服务的回调。
            audit_store: 审计存储；``None`` 表示不落审计（测试与无库场景）。

        Raises:
            ValueError: 任一必需依赖为 ``None``。
        """
        for name, value in (
            ("config", config),
            ("thread_store", thread_store),
            ("skill_store", skill_store),
            ("model_registry", model_registry),
            ("knowledge_provider", knowledge_provider),
        ):
            if value is None:
                raise ValueError(f"{name} 不能为 None")
        self._config = config
        self._thread_store = thread_store
        self._skill_store = skill_store
        self._model_registry = model_registry
        self._knowledge_provider = knowledge_provider
        self._audit_store = audit_store
        self._cache: dict[str, SessionServices] = {}
        # WHY 用锁而不是假定装配无并发：同一个新根可能被两条会话同时首次命中，
        # 而装配里包含「重建技能视图」这种会写盘的全量动作——并行跑两遍不只是浪费。
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 解析

    def managed_root(self, thread_id: str, *, preset: str = "") -> SessionRoot:
        """返回某条**未绑定工作空间**的会话的专属根（不创建目录）。"""
        from config import SessionRoot

        return SessionRoot(self._config, self._config.session_dir(thread_id), preset)

    def user_root(self, value: str | Path, *, preset: str = "") -> SessionRoot:
        """返回用户指定的工作空间根。

        Raises:
            ValueError: 路径不存在或不是目录。
        """
        from config import SessionRoot

        return SessionRoot(self._config, resolve_user_path(value), preset)

    def _requested_root(
        self, requested: str | None, *, preset: str = ""
    ) -> SessionRoot | None:
        """把请求里的取值解析成根；未给出（或空白）时返回 ``None``。"""
        if requested is None or (isinstance(requested, str) and not requested.strip()):
            return None
        return self.user_root(requested, preset=preset)

    def _check_preset(self, thread_id: str, stored: str, requested: str | None) -> None:
        """校验本次请求给的场景与库里已锁定的场景一致。

        WHY 与文件根分开校验、而不是复用 ``SessionRootLockedError``：两者的处置都是「新建
        会话」，但冲突对象不同（目录 vs 场景）。混成一条提示会让用户去改工作空间路径，然后
        发现还是失败。

        WHY 允许「补选」：库里为空表示这条会话还没选过场景（首轮没选），此时给出场景是被允许
        的——场景只决定技能集，不会让已有产物失联，与文件根那种「换了就找不到文件」的风险
        不同。
        """
        wanted = (requested or "").strip()
        if stored and wanted and stored != wanted:
            raise SessionPresetLockedError(f"会话 {thread_id}", stored, wanted)

    def _ready(self, root: SessionRoot, *, owned: bool) -> SessionRoot:
        """把「应用自己建的根」按需建出来，把「用户的根不见了」如实报成错误。

        WHY 必须在这里做、且必须返回根：下游（Agent 装配、Backend、附件索引、文件面板）
        都假定根存在；而会话专属目录在第一次用到它之前并不存在。少了这一步，打开一条
        「尚未发过消息、也没选工作空间」的会话会在构造 Backend 时抛出一个纯内部的
        ``NotADirectoryError`` —— 用户看到的是 500，且提示与他的操作毫无关系。

        Args:
            root: 已解析出的根。
            owned: 这个根是否**由应用拥有**（即它是按会话 ID 派生的专属目录）。用户选定
                的工作空间传 ``False``。

        Returns:
            可用的根（同一个对象）。

        Raises:
            SessionRootUnavailableError: 用户的目录不见了，或专属目录创建失败。
        """
        if root.root.is_dir():
            return root

        if not owned:
            # 用户选定的目录不见了：不替他重建（同名空目录会让「项目被清空」看起来成真）。
            logger.error("会话根不可用（用户选定的目录已不存在）：%s", root.root)
            raise SessionRootUnavailableError(root.root)

        try:
            root.ensure_directories()
        except OSError as exc:
            # 建不出来（权限、磁盘满、父目录被换成文件）同样是「根不可用」，只是原因不同；
            # 带上原因，用户才知道该修哪一样。
            logger.exception("会话专属目录创建失败：%s", root.root)
            raise SessionRootUnavailableError(root.root, f"创建失败：{exc}") from exc
        logger.info("会话专属目录已按需创建：%s", root.root)
        return root

    async def resolve(
        self,
        *,
        requested: str | None = None,
        thread_id: str | None = None,
        record: dict | None = None,
        allow_missing: bool = False,
        preset: str | None = None,
    ) -> SessionRoot:
        """解析本次请求应使用的会话根。

        规则（按优先级）：
        1. 未给 ``thread_id``：用 ``requested``；都没给就**没有根**（见
           ``SessionRootNotReadyError``）。
        2. 给了 ``thread_id`` 但该会话尚未登记（或还没有根）：用 ``requested``；
           没给则用它的**专属目录**（由 ID 派生，与第一轮交互登记的根是同一个）。
        3. 已有根（锁定）：一律以**库里那个**为准；``requested`` 与它不同则拒绝。

        WHY 锁定判据是「库里存了根」而不是「轮次 > 0」：根是在第一轮交互的那条
        UPSERT 里写死的（与轮次累加同一条语句），两者天然同步，用哪个判都一样；而
        「存了根」这个判据对「导入产生的会话」也成立。

        WHY 返回前一定过一遍 :meth:`_ready`：**解析出来的根必须可用**。返回一个磁盘上
        不存在的路径，等于把「这个根还不存在」这件正常的事，变成一个发生在下游某处的
        崩溃——而那里离「哪个根」这件事已经很远了。

        Args:
            requested: 请求里给出的工作空间路径；``None`` 表示不指定。
            thread_id: 会话 ID；``None`` 表示与会话无关的请求（如草稿态的面板）。
            record: 已经读到的会话元数据；给了就不再查一次库。
            allow_missing: 会话尚未登记时是否放行。**只有「会话到来之前」的动作才该
                传 True**（附件上传、首条消息的附件构造、草稿态面板）：那时这条会话在
                库里还不存在，而它将要使用的根是 ``requested``，没给就是它的专属目录。

        Returns:
            本次请求生效的会话根（目录已存在）。

        Raises:
            NotFoundError: 给了 ``thread_id`` 但该会话不存在（且 ``allow_missing=False``）。
            ValueError: ``requested`` 指向的目录不存在或不是目录。
            SessionRootLockedError: ``requested`` 与该会话已锁定的根不一致。
            SessionPresetLockedError: ``preset`` 与该会话已锁定的场景不一致。
            SessionRootNotReadyError: 本次请求**既没有会话 ID 也没有工作空间**
                （草稿态面板），因此无从给出一个根。
            SessionRootUnavailableError: 会话的根已确定，但目录不可用。
        """
        if thread_id is None:
            root = self._requested_root(requested, preset=preset or "")
            if root is None:
                raise SessionRootNotReadyError(
                    "这次请求还没有文件根：既没有指定会话，也没有给出工作空间。"
                    "请先选择工作空间，或先发出第一条消息"
                )
            return self._ready(root, owned=False)

        current = record
        if current is None:
            current = await self._thread_store.get(thread_id)
        if current is None:
            if not allow_missing:
                raise NotFoundError("会话", thread_id)
            root = self._requested_root(requested, preset=preset or "")
            if root is not None:
                return self._ready(root, owned=False)
            # 会话尚未登记，但**号已经发出去了**（前端在首次发送前先申请 ID），它将要使用的
            # 专属目录正由这个 ID 派生——而第一轮交互登记的也会是同一个根（记录里
            # ``workspace`` 为空 → 解析回同一个专属目录）。
            #
            # WHY 不能在这里说「还没有根」：这条路径正是「不选工作空间」的正常走法——首条
            # 消息的附件构造与运行前的根解析都会经过它，于是那句 409 会以「发送失败」的
            # 形式出现，而它给的下一步（「请先发出第一条消息」）照着做也出不去。
            return self._ready(self.managed_root(thread_id, preset=preset or ""), owned=True)

        stored = str(current.get("workspace") or "")
        stored_preset = str(current.get("preset") or "")
        if stored:
            # WHY 场景取自**库**而不是本次请求：场景与根一起锁定，此后这条会话的技能集
            # 只由库里的取值决定——否则「换一条会话打开同一个工作空间」会带着另一个场景
            # 重建视图，把前一条会话的技能集覆盖掉。
            #
            # WHY 库里为空时采用本次请求：那是「首轮没选场景、之后补选」这条正常路径
            # （``_check_preset`` 只在两边都非空且不同时报冲突）。取库里的空串会让补选
            # 永远不生效——表现为「我明明选了场景，Agent 还是那套技能」。
            effective_preset = stored_preset or (preset or "").strip()
            locked = self.from_stored(stored, preset=effective_preset)
            root = self._requested_root(requested, preset=preset or "")
            if root is not None and str(root.root) != str(locked.root):
                raise SessionRootLockedError(thread_id, str(locked.root), str(root.root))
            self._check_preset(thread_id, stored_preset, preset)
            # 锁定的根是不是「应用自己建的」由建它时的选择决定，事后无法从路径推断：
            # 用户完全可以把工作空间选在 SESSIONS_ROOT 里面。
            return self._ready(locked, owned=not bool(current.get("workspace_bound")))

        # 还没有根：本次请求给的（或它的专属目录）就是它将要锁定的那一个。
        self._check_preset(thread_id, stored_preset, preset)
        chosen = self._requested_root(requested, preset=preset or "")
        if chosen is not None:
            return self._ready(chosen, owned=False)
        return self._ready(self.managed_root(thread_id, preset=preset or ""), owned=True)

    def from_stored(self, stored: str, *, preset: str = "") -> SessionRoot:
        """把库里存的根还原成 :class:`SessionRoot`（**不**做存在性校验）。

        WHY 不校验：那是这条会话此前实际用过的目录。目录被删掉时应当让调用方看到
        「目录不存在」这种具体错误（文件面板会给出），而不是在这里被拒绝解析——
        拒绝会让用户连自己的历史会话都打不开。

        WHY 连场景一起还原：场景与根在同一条记录里锁定；只还原 root 会让技能视图按
        「不限定」重建，表现为「历史会话一打开，技能集就变了」。
        """
        from config import SessionRoot

        return SessionRoot(self._config, Path(stored), preset)

    async def describe(
        self,
        *,
        thread_id: str | None = None,
        requested: str | None = None,
        allow_missing: bool = True,
    ) -> WorkspaceInfo:
        """给界面用的根信息：路径、名字，以及「是用户选的还是应用建的」「锁没锁」。

        WHY 单独一个方法而不是让文件面板服务回答：这两件事分别来自库里那条记录与本
        模块的解析规则，而文件面板只知道自己那个根。让面板去查库，等于把「归属」这条
        规则复制到第二个地方。

        Raises:
            SessionRootNotReadyError: 会话还没有根。
            SessionRootUnavailableError: 会话的根已确定，但目录不可用（用户选定的目录不见了）。
            ValueError: ``requested`` 指向的目录不存在或不是目录。
        """
        # WHY 不再自己判存在性：``resolve`` 已经保证交出来的根可用（专属目录按需建、
        # 用户目录不见了则报错）。同一件事写在两处，迟早会出现「一处建、一处报错」的分歧。
        root = await self.resolve(
            requested=requested, thread_id=thread_id, allow_missing=allow_missing
        )
        record = await self._thread_store.get(thread_id) if thread_id else None
        stored = str((record or {}).get("workspace") or "")
        locked = bool(stored)
        if locked:
            bound = bool((record or {}).get("workspace_bound"))
        else:
            # 还没锁定：是「用户这次选了工作空间」还是「将要用专属目录」取决于请求。
            bound = requested is not None and str(requested).strip() != ""
        return WorkspaceInfo(
            path=str(root.root),
            name=root.root.name or str(root.root),
            bound=bound,
            locked=locked,
        )

    # ------------------------------------------------------------------ 装配

    async def services(self, root: SessionRoot) -> SessionServices:
        """取某个根的服务集合，首次调用时装配。

        WHY 在这里 ``ensure_directories()``：目录布局（``skills/``、附件目录）是**选中
        这个根的时候**才该建的，放到启动时会把每个候选目录都改一遍——而用户的工作空间
        是他的仓库，不是我们的；会话专属目录更不该在没发过消息时就凭空出现。

        Args:
            root: 目标会话根。

        Returns:
            该根的服务集合（场景兼容时复用同一个对象；场景由「未定」变为具体时会重建）。

        Raises:
            ValueError: ``root`` 为 ``None``。
            SessionPresetLockedError: 两边都是**具体**场景且不同（同一个工作空间只能有一个）。
            OSError: 目录创建或技能视图重建失败。
        """
        if root is None:
            raise ValueError("root 不能为 None")
        key = str(root.root)

        cached = self._cache.get(key)
        if cached is not None and self._preset_allows_reuse(cached.root, root):
            return cached

        async with self._lock:
            # 双检：并发首次命中时，等锁期间可能已被另一个协程装配好
            cached = self._cache.get(key)
            if cached is not None and self._preset_allows_reuse(cached.root, root):
                return cached
            bundle = await self._assemble(root)
            self._cache[key] = bundle
            return bundle

    def _preset_allows_reuse(self, cached: SessionRoot, root: SessionRoot) -> bool:
        """已装配的服务能否直接复用（返回 ``False`` = 按 ``root`` 重建技能视图）。

        WHY 不能只判「场景是否相等」：技能视图按**工作空间**物化，而场景在此之前可能还没
        定下来——草稿态的面板与附件解析都会先按「不限定」装配一次（首条消息的附件那一步就
        会调 ``resolve_scoped_services``）。把那次临时装配当成一条既定事实，用户第一次提交
        场景就会撞 409，而他什么都没做错。三种情形：

        1. 场景相同 → 复用（幂等；客户端每轮都带同一个值很常见）；
        2. 已装配的那份是「不限定」→ **不复用**（重建）：它只是"场景还没定下来"时的临时装配，
           不是事实；重建顺带把视图对齐到刚确定的场景——少了这一步，预设里的技能一个都
           进不了该会话的上下文，而日志上看不出任何异常；
        3. 本次请求是「不限定」而库里已有具体场景 → **复用**（连同它那个场景）：这个工作空间的
           视图已经是那个场景的，重建会把正在用它的会话的技能集换掉（技能索引每会话只加载
           一次，换了不报错、只是行为变了）。

        两边都是具体场景且不同时**报错而不是重建**：视图按根一份，重建等于把另一条会话的
        技能集换掉——那正是 ``SessionPresetLockedError`` 要挡的事。
        """
        if cached.preset == root.preset:
            return True
        if not cached.preset:
            return False
        if not root.preset:
            return True
        raise SessionPresetLockedError(f"工作空间 {root.root}", cached.preset, root.preset)

    async def _assemble(self, root: SessionRoot) -> SessionServices:
        """真正装配一个根（调用方必须已持有 ``_lock``）。"""
        root.ensure_directories()
        # WHY 在这里建存储目录（技能库 / 技能视图 / 工具留存）：它们物理上就在工作区内的
        # ``.harness/`` 下（2026-09-22 起），所以「往用户项目里写东西」只发生在这个目录里
        # ——它由面板隐藏、经只读路由 ``/.harness`` 暴露，用户在自己的仓库里不会撞见半成品。
        root.ensure_storage()

        skills = SkillService(self._config, scope=root, store=self._skill_store)
        # WHY 装配时就把视图建好：图里烧进去的技能来源是挂载出来的 ``/.skills-active``，
        # 而这份派生物是「视图存在 → 用它，不存在 → 退回配置目录（等于全部启用）」
        # 的判定基础。换了根却不重建，Agent 会按上一个根的技能集做事——而技能索引
        # 每会话只加载一次，错了不会自愈。
        await skills.refresh_view()

        bundle = SessionServices(
            root=root,
            files=WorkspaceService(self._config, scope=root, audit_store=self._audit_store),
            attachments=AttachmentService(
                self._config,
                scope=root,
                registry=self._model_registry,
                thread_store=self._thread_store,
                audit_store=self._audit_store,
            ),
            skills=skills,
            knowledge=await self._knowledge_provider(root),
        )
        logger.info("会话根服务已装配：root=%s", root.root)
        return bundle

    async def services_for(
        self,
        *,
        requested: str | None = None,
        thread_id: str | None = None,
        record: dict | None = None,
        allow_missing: bool = False,
        preset: str | None = None,
    ) -> SessionServices:
        """解析并装配一步到位，供各接口端点使用。

        Raises:
            NotFoundError: 给了 ``thread_id`` 但该会话不存在（且 ``allow_missing=False``）。
            ValueError: ``requested`` 指向的目录不存在或不是目录。
            SessionRootLockedError: ``requested`` 与该会话已锁定的根不一致。
            SessionPresetLockedError: ``preset`` 与该会话已锁定的场景不一致。
            SessionRootNotReadyError: 会话还没有根，且没有给出可用取值。
        """
        root = await self.resolve(
            requested=requested,
            thread_id=thread_id,
            record=record,
            allow_missing=allow_missing,
            preset=preset,
        )
        return await self.services(root)

    # ------------------------------------------------------------------ 挑选目录

    def pick_folder(self, initial: str | None = None) -> str | None:
        """在服务端弹出系统文件夹选择对话框，返回选中的目录（取消时 ``None``）。

        Raises:
            ValueError: 选中的路径不存在或不是目录。
            FolderPickerUnavailableError: 环境不支持（无桌面 / 缺 tkinter）。
            FolderPickerBusyError: 已经有一个弹窗在等待操作。
            FolderPickerTimeoutError: 超时未关闭。
        """
        return choose_workspace_folder(self._config, initial)

    def list_directories(self, path: str | None = None) -> DirectoryListing:
        """列出某个目录下的子目录（供界面逐级挑选）。

        Raises:
            ValueError: ``path`` 不存在或不是目录。
        """
        return list_directories(path)

    # ------------------------------------------------------------------ 观测

    def cached_roots(self) -> list[str]:
        """已装配的根目录（去重保序），供排错与测试使用。"""
        return list(self._cache)

    async def refresh_views(self) -> list[str]:
        """按当前启用状态重建**所有已装配根**的技能视图，返回重建的根目录列表。

        WHY 需要它：技能启停是全局的（按技能名一条记录），而视图是按根各一份的派生物。
        启停接口只重建了它自己那个根的视图，其余根会停留在旧内容上——表现为「在 A 会话里
        停用了某技能，切到 B 会话它还在」。由调用方（技能服务的写路径）触发全部重建，
        才能让「库是真相」在整进程范围内成立。
        """
        rebuilt: list[str] = []
        for key in list(self._cache):
            bundle = self._cache.get(key)
            if bundle is None:
                continue
            await bundle.skills.refresh_view()
            rebuilt.append(key)
        return rebuilt
