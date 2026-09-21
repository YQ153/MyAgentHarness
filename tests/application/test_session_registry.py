"""会话根的解析、锁定与装配。

本文件钉的是新模型的四条规则（其余用例验的是它们在各层里的落地）：

1. 会话可以不绑定工作空间——那时它的根是应用为它建的**专属目录**；
2. 会话可以绑定任意目录（没有允许清单），同一个工作空间能容纳多条会话；
3. 根在**产生第一条交互**时锁定，此后给出不同取值直接拒绝；
4. 「锁定」与「还没确定」是两种不同状态，界面必须能分辨（``describe`` 的
   ``bound`` / ``locked``）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from application.errors import (
    SessionRootLockedError,
    SessionRootNotReadyError,
    SessionRootUnavailableError,
)
from application.session_registry import SessionRegistry, list_directories
from config import SessionRoot
from runtime.thread_store import ThreadMetaStore, open_thread_store
from tests.conftest import make_config


class _SkillState:
    """技能启停状态替身：只实现装配路径真正读的那一个方法。

    WHY 用最小替身而不是真存储：本文件验的是「根怎么解析、服务怎么按根装配」，技能库里
    有什么技能与它无关；拉起真存储只会让失败原因混进与用例意图无关的磁盘依赖。
    """

    async def enabled_map(self, *, scope: str = "global") -> dict[str, bool]:
        del scope
        return {}

    async def resolve(self, *args: Any, **kwargs: Any) -> Any:
        """重建视图时会按作用域解析一次状态；本文件不关心内容，给空即可。"""
        del args, kwargs
        return {}


class _ModelRegistry:
    """模型注册表替身：附件服务在构造时只把它存起来，不会立刻使用。"""


def _registry(
    config: Any,
    thread_store: ThreadMetaStore,
    *,
    knowledge: Any = None,
) -> SessionRegistry:
    """构造注册表；知识库用哨兵顶替（本文件不验知识库本身）。"""
    sentinel = knowledge if knowledge is not None else object()

    async def _provider(_root: SessionRoot) -> Any:
        return sentinel

    return SessionRegistry(
        config,
        thread_store=thread_store,
        skill_store=_SkillState(),
        model_registry=_ModelRegistry(),
        knowledge_provider=_provider,
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[ThreadMetaStore]:
    async with open_thread_store(tmp_path / "threads.db") as opened:
        yield opened


# --------------------------------------------------------------- 1. 不绑定 → 专属目录


async def test_unbound_session_gets_its_own_directory(
    tmp_path: Path, store: ThreadMetaStore
) -> None:
    """没选工作空间的会话，根是 ``<SESSIONS_ROOT>/<thread_id>``。

    WHY 这是「不绑定」那条路的全部含义：它同样需要一个文件根（文件工具、沙箱挂载根、
    附件目录与知识库索引都以它为准），只是这个根不属于任何项目，只有这条会话在用。
    """
    config = make_config(tmp_path)
    registry = _registry(config, store)

    root = await registry.resolve(thread_id="t1", record={})

    assert root.root == config.session_dir("t1")
    assert str(root.root).startswith(str(config.resolved_sessions_root))


async def test_two_unbound_sessions_do_not_share_a_directory(
    tmp_path: Path, store: ThreadMetaStore
) -> None:
    """两条未绑定的会话各自一个目录。

    WHY 用会话 ID 当子目录名：共享同一个目录会让两条毫不相关的会话互相看到对方的产物，
    而那是「我什么都没做，它却读到了别人的文件」这类问题的来源。
    """
    config = make_config(tmp_path)
    registry = _registry(config, store)

    first = await registry.resolve(thread_id="t1", record={})
    second = await registry.resolve(thread_id="t2", record={})

    assert first.root != second.root


async def test_resolve_creates_the_managed_directory(tmp_path: Path, store: ThreadMetaStore) -> None:
    """专属目录在**解析时**就建出来，而不是等某个调用方想起来建。

    WHY 单列（回归）：下游全都假定「拿到的根存在」——Agent 装配、Backend、附件索引、
    文件面板都是如此。专属目录此前只由服务装配（``services()``）创建，于是**不经过装配
    的那条路径**（读历史：它只需要图与附件索引）会拿着一个不存在的根走到 Backend 里那条
    「根必须是已存在的目录」的断言上，用户点开自己的会话看到的是 500。
    """
    config = make_config(tmp_path)
    registry = _registry(config, store)
    await store.create("t1")
    assert not config.session_dir("t1").exists()

    root = await registry.resolve(thread_id="t1")

    assert root.root == config.session_dir("t1")
    assert root.root.is_dir()


async def test_unregistered_thread_gets_its_own_directory(
    tmp_path: Path, store: ThreadMetaStore
) -> None:
    """尚未登记的会话（号已发出）解析得到它的**专属目录**，而不是「还没有根」。

    WHY 单列（回归）：首条消息的到达顺序是「先申请 ID → 再发消息」，而运行前的根解析与
    附件构造都在会话登记**之前**发生（它们要 ``allow_missing=True``）。此前这一支报 409
    「这条会话还没有专属目录……请先发出第一条消息」——于是「不选工作空间」这条正常路径
    被自己的提示挡在门外，用户照着做（再发一条）也出不去。
    """
    config = make_config(tmp_path)
    registry = _registry(config, store)

    root = await registry.resolve(thread_id="t1", allow_missing=True)

    assert root.root == config.session_dir("t1")
    assert root.root.is_dir(), "解析时就该把专属目录建出来"
    # 与第一轮交互登记的根必须一致：否则首条消息会写进一个目录、而会话此后指向另一个。
    await store.record_turn("t1", title_hint="你好", turn_delta=1)
    assert (await registry.resolve(thread_id="t1")).root == root.root


async def test_resolve_still_reports_not_ready_without_any_id(
    tmp_path: Path, store: ThreadMetaStore
) -> None:
    """连会话 ID 都没有（草稿态面板）：仍然如实说「还没有根」。

    WHY 与上一条成对：去掉「未登记」那支的 409 不等于把这个错误关掉——没有 ID 就没有可
    派生的目录，此时退回某个默认目录会让用户在「以为在看自己的项目」的面板里看到别处。
    """
    config = make_config(tmp_path)
    registry = _registry(config, store)

    with pytest.raises(SessionRootNotReadyError):
        await registry.resolve(thread_id=None, allow_missing=True)


@pytest.mark.parametrize(
    "bad_id",
    ["..", ".", "../evil", "..\\evil", "a/b", "a\\b", "   "],
    ids=["dotdot", "dot", "posix-parent", "windows-parent", "posix-sep", "windows-sep", "blank"],
)
def test_session_dir_only_accepts_a_single_safe_segment(tmp_path: Path, bad_id: str) -> None:
    """专属目录名只能是一个安全片段。

    WHY 单列（安全）：这个目录名直接来自请求——前端先申请一个 ID，服务端按它派生目录。
    ``Path(sessions_root) / "../../evil"`` 会解析到会话目录之外，于是 Agent 的文件根、
    附件与产物全部落在别处，且没有任何提示。拒绝而不是「清洗」：清洗会让两个不同的 ID
    撞进同一个目录，那比报错更危险。
    """
    config = make_config(tmp_path)

    with pytest.raises(ValueError):
        config.session_dir(bad_id)


async def test_a_traversal_id_creates_nothing_outside_the_sessions_root(
    tmp_path: Path, store: ThreadMetaStore
) -> None:
    """越界 ID 走**完整解析路径**也不得在工作区之外建出任何目录。

    WHY 不只测 ``session_dir``：真正的风险在「解析 → 按需建目录」（``_ready``）这条链上，
    单测目录名只是其中一环；这里断言的是**没有副作用**——抛错前就 mkdir 掉父目录，等于把
    越界写盘的后果留下了一半。
    """
    config = make_config(tmp_path)
    registry = _registry(config, store)
    outside = tmp_path / "evil"

    with pytest.raises(ValueError):
        await registry.resolve(thread_id="../evil", allow_missing=True)

    assert not outside.exists()


async def test_resolve_does_not_recreate_a_vanished_workspace(
    tmp_path: Path, store: ThreadMetaStore
) -> None:
    """用户选定的目录不见了：报错，且**不**替他建一个同名空目录。

    WHY 不「顺手建出来」：那是他选定的项目目录。建一个空目录会让 Agent 在里面「找不到
    文件」并写下新文件，而用户看到的现象是「我的项目被清空了」——比一次明确的失败危险
    得多。要恢复的是那个目录，不是同名的空壳。
    """
    config = make_config(tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    registry = _registry(config, store)
    await store.create("t1", workspace=str(workspace.resolve()), workspace_bound=True)
    workspace.rmdir()

    with pytest.raises(SessionRootUnavailableError, match="不可用"):
        await registry.resolve(thread_id="t1")

    assert not workspace.exists(), "不该替用户重建目录"


async def test_describe_reports_a_vanished_workspace_as_unavailable(
    tmp_path: Path, store: ThreadMetaStore
) -> None:
    """面板问「我在哪个根」时，根不可用要按同一种失败说出来（而不是一个裸 500）。

    WHY：界面文案与状态码都取决于这个事实。以前这里抛的是 ``RuntimeError``（映射成 500），
    用户看到「服务端故障」，而实际要做的是把目录恢复回来。
    """
    config = make_config(tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    registry = _registry(config, store)
    await store.create("t1", workspace=str(workspace.resolve()), workspace_bound=True)
    workspace.rmdir()

    with pytest.raises(SessionRootUnavailableError):
        await registry.describe(thread_id="t1")


# --------------------------------------------------------------- 2. 绑定任意目录 / 多会话


async def test_any_existing_directory_can_be_bound(tmp_path: Path, store: ThreadMetaStore) -> None:
    """任意已存在的目录都能被绑定——没有允许清单。

    WHY 单独钉：这是产品规则（工作空间可由用户任意选择）。以前那道「只能选清单内目录」
    的边界如果被无意恢复，这条会立刻变红。
    """
    config = make_config(tmp_path)
    chosen = tmp_path / "anywhere" / "deep" / "project"
    chosen.mkdir(parents=True)
    registry = _registry(config, store)

    root = await registry.resolve(requested=str(chosen), thread_id="t1", record={})

    assert root.root == chosen.resolve()


async def test_one_workspace_hosts_several_sessions(tmp_path: Path, store: ThreadMetaStore) -> None:
    """同一个工作空间可以承载多条会话，且它们共用同一份服务（只装配一次）。

    WHY 共用装配：四类服务都以根为键。按会话各建一份会让「同一条会话两次请求拿到两份
    缓存」成为常态——技能物化视图会被反复重建，知识库会各开一个连接。
    """
    config = make_config(tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    registry = _registry(config, store)

    first = await registry.resolve(requested=str(workspace), thread_id="t1", record={})
    second = await registry.resolve(requested=str(workspace), thread_id="t2", record={})
    bundle_a = await registry.services(first)
    bundle_b = await registry.services(second)

    assert first.root == second.root == workspace.resolve()
    assert bundle_a is bundle_b
    assert registry.cached_roots() == [str(workspace.resolve())]


async def test_a_missing_directory_is_rejected(tmp_path: Path, store: ThreadMetaStore) -> None:
    """不存在的目录被拒绝，且是**当场**拒绝。

    WHY 不能放行：放行的话 ``ensure_directories`` 会把这个拼错的路径「成功」地变成
    一个空目录——用户要到第一次让 Agent 找文件时才发现自己指错了地方。
    """
    config = make_config(tmp_path)
    registry = _registry(config, store)

    with pytest.raises(ValueError, match="不存在|不是目录"):
        await registry.resolve(requested=str(tmp_path / "typo"), thread_id="t1", record={})


# --------------------------------------------------------------- 3. 锁定


async def test_root_locks_after_the_first_turn(tmp_path: Path, store: ThreadMetaStore) -> None:
    """库里存了根之后，给出**不同**取值直接拒绝；相同取值或什么都不给则放行。

    WHY 锁定时刻是「产生第一条交互」：创建会话时用户还在选，允许改主意；而一旦 Agent
    已经在那个根里读过或写过文件，再换就会让此前的产物失联——且不会报错。
    """
    config = make_config(tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    other = tmp_path / "another"
    other.mkdir()
    registry = _registry(config, store)
    locked = {"workspace": str(workspace.resolve()), "workspace_bound": 1}

    with pytest.raises(SessionRootLockedError):
        await registry.resolve(requested=str(other), thread_id="t1", record=locked)

    assert (await registry.resolve(requested=str(workspace), thread_id="t1", record=locked)).root == (
        workspace.resolve()
    )
    assert (await registry.resolve(thread_id="t1", record=locked)).root == workspace.resolve()


async def test_a_locked_managed_root_cannot_be_replaced(
    tmp_path: Path, store: ThreadMetaStore
) -> None:
    """锁定到「专属目录」的会话同样不能被改成某个工作空间。

    WHY 单独钉：那只发生在「用户没选 → 发出第一条消息 → 又想把会话指到某个项目」这条
    路径上。它必须与「锁定到工作空间」一样被拒绝，否则那条路径会静默换根。
    """
    config = make_config(tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    registry = _registry(config, store)
    managed = str(config.session_dir("t1"))
    locked = {"workspace": managed, "workspace_bound": 0}

    with pytest.raises(SessionRootLockedError):
        await registry.resolve(requested=str(workspace), thread_id="t1", record=locked)

    assert (await registry.resolve(thread_id="t1", record=locked)).root == Path(managed)


async def test_first_turn_without_a_workspace_binds_the_managed_directory(
    tmp_path: Path, store: ThreadMetaStore
) -> None:
    """首轮不给工作空间 → 锁定到专属目录；``describe`` 如实说是「应用建的」。

    这是最常见的默认路径：用户直接发消息。界面必须能把它与「用户选的工作空间」区分开，
    否则用户会以为自己的文件进了某个项目目录。
    """
    config = make_config(tmp_path)
    registry = _registry(config, store)
    await store.create("t1")

    root = await registry.resolve(thread_id="t1", record={"workspace": "", "workspace_bound": 0})
    info = await registry.describe(thread_id="t1")

    assert root.root == config.session_dir("t1")
    assert info.bound is False
    assert info.locked is False


# --------------------------------------------------------------- 4. describe


async def test_describe_distinguishes_bound_from_managed(
    tmp_path: Path, store: ThreadMetaStore
) -> None:
    """``bound`` 区分「用户选的」与「应用建的」，``locked`` 回答「还能不能换」。

    WHY 单独存这个事实：路径本身不携带它（用户完全可以把工作空间选在 sessions 目录里），
    而界面文案与用户对自己文件的预期都取决于它。
    """
    config = make_config(tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    registry = _registry(config, store)
    # 未绑定的会话必须是**已登记**的：专属目录按会话 ID 派生，而没有那条记录时
    # 服务端无从知道该用哪个 ID（那是「还没有根」而不是「用专属目录」）。
    await store.create("t2")

    bound = await registry.describe(thread_id="t1", requested=str(workspace))
    managed = await registry.describe(thread_id="t2")

    assert (bound.bound, bound.locked) == (True, False)
    assert bound.path == str(workspace.resolve())
    assert (managed.bound, managed.locked) == (False, False)
    assert managed.path == str(config.session_dir("t2"))


async def test_describe_reports_not_ready_for_a_draft_without_a_choice(
    tmp_path: Path, store: ThreadMetaStore
) -> None:
    """草稿态又没选工作空间时，如实说「还没有根」。

    WHY 不悄悄退到某个默认目录：那会让用户在「以为在看自己的项目」的面板里看到应用
    自己的目录，而两边都不报错。
    """
    config = make_config(tmp_path)
    registry = _registry(config, store)

    with pytest.raises(SessionRootNotReadyError):
        await registry.describe(thread_id=None, requested=None)


async def test_services_rejects_none_root(tmp_path: Path, store: ThreadMetaStore) -> None:
    """``services(None)`` 直接报错，而不是悄悄用某个默认根。"""
    config = make_config(tmp_path)
    registry = _registry(config, store)

    with pytest.raises(ValueError, match="root"):
        await registry.services(None)  # type: ignore[arg-type]


# --------------------------------------------------------------- 目录挑选


def test_list_directories_lists_one_layer(tmp_path: Path) -> None:
    """列目录只列一层、只列目录。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")

    view = list_directories(str(tmp_path))

    assert [item.name for item in view.entries] == ["a", "b"]
    assert view.path == str(tmp_path.resolve())


def test_list_directories_rejects_a_missing_path(tmp_path: Path) -> None:
    """不存在的路径报「不存在」，而不是回一个空列表。

    WHY：空列表会被理解成「这个目录是空的」——两者的下一步动作完全不同。
    """
    with pytest.raises(ValueError, match="不存在"):
        list_directories(str(tmp_path / "gone"))


def test_list_directories_walks_up_to_the_file_system_top(tmp_path: Path) -> None:
    """一路向上最终停在文件系统根，而不是原地打转。

    WHY 专门钉：Windows 盘符根的 ``parent`` 是它自己，直接回传会让「上一级」变成原地
    不动——按钮看起来有效、点下去什么也不发生。
    """
    view = list_directories(str(tmp_path))

    for _ in range(64):
        if view.parent is None:
            break
        assert view.parent != view.path, "上一级指向了自己"
        view = list_directories(view.parent)
    else:
        pytest.fail("一路向上都没有终止：父目录链没有尽头")

    assert view.parent is None


def test_list_directories_starts_at_the_top(tmp_path: Path) -> None:
    """不传路径时从顶层开始（Windows 是盘符列表，POSIX 是 ``/``）。"""
    view = list_directories(None)

    # 只有一个起点（POSIX 的 /，或只有一个盘符的机器）时直接进到它里面
    assert view.path == "" or view.parent is None


# --------------------------------------------------------------- 系统弹窗


class _Picker:
    """弹窗替身：真弹窗会在跑测试的机器上开一个没人关的窗口。"""

    def __init__(self, result: Path | None) -> None:
        self.result = result
        self.initial: list[Path | None] = []

    def __call__(self, *, initial_dir: Path | None = None, **kwargs: Any) -> Path | None:
        del kwargs
        self.initial.append(initial_dir)
        return self.result


def test_choose_workspace_folder_returns_the_picked_path(tmp_path: Path) -> None:
    """弹窗选中 → 返回绝对路径。"""
    from application.session_registry import choose_workspace_folder

    chosen = tmp_path / "picked"
    chosen.mkdir()
    config = make_config(tmp_path)

    result = choose_workspace_folder(config, picker=_Picker(chosen))

    assert result == str(chosen.resolve())


def test_choose_workspace_folder_returns_none_on_cancel(tmp_path: Path) -> None:
    """取消 → ``None``（不是错误）。"""
    from application.session_registry import choose_workspace_folder

    config = make_config(tmp_path)

    assert choose_workspace_folder(config, picker=_Picker(None)) is None


def test_choose_workspace_folder_rejects_a_vanished_path(tmp_path: Path) -> None:
    """弹窗返回一个已消失的路径时报错，而不是把它当成有效选择。"""
    from application.session_registry import choose_workspace_folder

    config = make_config(tmp_path)

    with pytest.raises(ValueError, match="不存在"):
        choose_workspace_folder(config, picker=_Picker(tmp_path / "gone"))
