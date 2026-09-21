"""根外存储：技能库 / 技能视图 / 工具留存搬出工作区之后的位置、迁移与挂载。

WHY 需要这一组用例：这次改动的**验收点**是「工作区里不再出现应用自己的目录」——而这件事
一旦回退，表现只是「用户的项目里又多了几个名字」，没有任何报错。因此这里同时钉住四件事：

1. 位置：存储目录由根派生、稳定、且不与别的根撞车；
2. 不碰工作区：装配一个根之后，工作区里**只有用户自己的文件**；
3. 迁移：旧位置（工作区里的 ``skills/``）的技能包要搬过来（技能是用户放进来的东西，
   静默丢掉等于让它凭空消失）；
4. 挂载：三个虚拟路径经只读挂载接回，Agent 与文件面板都读得到。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from langgraph.store.base import BaseStore

from agent.backends import build_backend
from application.session_registry import SessionRegistry
from config import VIRTUAL_SKILLS, VIRTUAL_SKILL_VIEW, VIRTUAL_TOOL_OUTPUTS, SessionRoot
from runtime.store import open_store
from runtime.thread_store import ThreadMetaStore
from runtime.tool_outputs import tool_output_path, tool_output_virtual_path, write_tool_output
from runtime.workspace_files import resolve_in_workspace
from tests.application.test_session_registry import _registry
from tests.conftest import make_config, make_root

_SKILL = "---\nname: {name}\ndescription: {name} 的说明\n---\n\n# {name}\n"

_STORED_NAMES = ("skills", "skills-active", "tool-outputs")
"""存储目录里的三个子目录名（见 ``config`` 的布局常量）。"""


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[BaseStore]:
    async with open_store(tmp_path / "agent.db") as opened:
        yield opened


def _write_skill(directory: Path, name: str) -> Path:
    """在 ``directory/<name>/SKILL.md`` 写一个技能包。"""
    package = directory / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(_SKILL.format(name=name), encoding="utf-8")
    return package


def _names(directory: Path) -> set[str]:
    """目录里的一级条目名（目录不存在时为空集）。"""
    return {item.name for item in directory.iterdir()} if directory.is_dir() else set()


# ------------------------------------------------------------------ 位置


def test_storage_dir_is_derived_from_the_root_and_stable(tmp_path: Path) -> None:
    """同一个根恒得到同一个存储目录；不同的根不撞车；名字可读。"""
    config = make_config(tmp_path)
    first = make_root(config, name="project-a")
    again = SessionRoot(config, first.root)
    other = make_root(config, name="project-b")

    assert first.storage_dir == again.storage_dir
    assert first.storage_dir != other.storage_dir
    assert first.storage_dir.parent == config.roots_store_root
    assert first.storage_dir.name.startswith("project-a-")


def test_the_storage_dir_does_not_depend_on_the_data_directory_layout(tmp_path: Path) -> None:
    """存储目录跟着数据目录走：换数据目录就换一片存储（备份只需搬一个目录）。"""
    left = make_root(make_config(tmp_path / "left"))
    right = make_root(make_config(tmp_path / "right"))

    assert left.storage_dir != right.storage_dir
    assert left.storage_dir.parent.parent == tmp_path / "left"


# ------------------------------------------------------------------ 不碰工作区


def test_ensure_storage_creates_the_stores_but_not_the_view(tmp_path: Path) -> None:
    """建技能库与留存目录，但**不**预建技能视图目录。

    WHY 视图目录不能预建：``sources_for_graph`` 用「视图目录是否存在」判断「物化视图是否
    建过」——预建一个空目录会让那个判据永远为真，于是「视图被清理 / 重建失败」与「所有
    技能都停用」再也分不开（前者会让技能静默消失且没有告警）。
    """
    root = make_root(make_config(tmp_path))

    root.ensure_storage()

    assert _names(root.storage_dir) == {"skills", "tool-outputs"}
    assert root.skill_view_store.exists() is False


async def test_assembling_a_root_leaves_the_workspace_untouched(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """装配一个根之后，工作区里**只有用户自己的文件**。

    WHY 这是本次改动的验收点：技能库、技能视图与工具留存以前住在工作区里，于是「用户挑一个
    仓库当工作空间」就等于「应用往里写三个名字」（污染他的版本控制）。回退这件事不会报错，
    只会在某天被人发现项目里多了目录。
    """
    config = make_config(tmp_path)
    root = make_root(config)
    (root.root / "README.md").write_text("用户自己的文件\n", encoding="utf-8")

    await _registry(config, thread_store).services_for(record={}, thread_id=None, requested=str(root.root))

    assert _names(root.root) == {"README.md"}, "工作区里不该出现应用自己的目录"
    assert _names(root.storage_dir) >= {"skills", "tool-outputs"}


async def test_the_skill_view_lands_in_storage_not_in_the_workspace(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """装配会建出技能视图——它必须落在存储目录里。"""
    config = make_config(tmp_path)
    root = make_root(config)

    await _registry(config, thread_store).services_for(record={}, thread_id=None, requested=str(root.root))

    assert root.skill_view_store.is_dir()
    assert not (root.root / ".skills-active").exists()


# ------------------------------------------------------------------ 迁移


async def test_legacy_skill_library_is_migrated_out_of_the_workspace(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """旧位置（工作区里的 ``skills/``）的技能包要搬进存储目录，旧目录随之消失。"""
    config = make_config(tmp_path)
    root = make_root(config)
    package = _write_skill(root.root / "skills", "code-review")

    await _registry(config, thread_store).services_for(record={}, thread_id=None, requested=str(root.root))

    assert (root.skills_store / "code-review" / "SKILL.md").is_file()
    assert not package.exists()
    assert not (root.root / "skills").exists()


async def test_an_empty_legacy_skill_library_is_removed(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """空的旧技能目录也要清掉：留着会让用户看到「说搬走了却还在」。"""
    config = make_config(tmp_path)
    root = make_root(config)
    (root.root / "skills").mkdir()

    await _registry(config, thread_store).services_for(record={}, thread_id=None, requested=str(root.root))

    assert not (root.root / "skills").exists()


async def test_configured_skill_dirs_are_left_alone(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """``SKILL_DIRS`` 显式指向工作区里的目录时：那是**用户配置的**路径，不能当旧位置搬走。

    WHY 单列（实测踩到）：迁移逻辑按「``<根>/skills`` 存在且有内容」判断，而用户完全可能
    正是把技能目录配在那儿——搬走会让配置里的路径凭空消失，症状是「技能一个都列不出来」。
    """
    config = make_config(tmp_path)
    root = make_root(config)
    package = _write_skill(root.root / "skills", "code-review")
    configured = make_config(tmp_path, skill_dirs=[root.root / "skills"])
    registry = _registry(configured, thread_store)

    await registry.services_for(record={}, thread_id=None, requested=str(root.root))

    assert package.is_dir(), "显式配置的技能目录不该被搬走"
    assert not (root.storage_dir / "skills").exists(), "显式配置时不用我们那份技能库"


# ------------------------------------------------------------------ 挂载


def test_mount_table_covers_the_three_virtual_paths(tmp_path: Path) -> None:
    """三个虚拟路径都挂在表上，并指向存储目录（巡检与面板都靠它）。"""
    root = make_root(make_config(tmp_path))

    table = root.mount_table

    assert table[f"{VIRTUAL_SKILLS}/"] == root.skills_store
    assert table[f"{VIRTUAL_SKILL_VIEW}/"] == root.skill_view_store
    assert table[f"{VIRTUAL_TOOL_OUTPUTS}/"] == root.tool_output_store


def test_the_builtin_skill_directory_is_mounted_too(tmp_path: Path) -> None:
    """内置技能目录（在应用目录里）也要挂上。

    WHY 单列：``sources_for_graph`` 的兜底（视图缺失 → 退回配置目录）只有在那些目录真的
    挂上时才成立，否则那句「等于全部启用」是假的——实测表现为读不到 + 一条 WARNING。
    """
    root = make_root(make_config(tmp_path))

    mounted = {mount.label for mount in root.read_only_mounts}

    assert "技能来源 /skills-builtin" in mounted


async def test_the_backend_exposes_the_skill_view_read_only(
    tmp_path: Path, store: BaseStore, thread_store: ThreadMetaStore
) -> None:
    """经真实 ``build_backend``：技能视图读得到，写不动。"""
    config = make_config(tmp_path)
    root = make_root(config)
    await _registry(config, thread_store).services_for(record={}, thread_id=None, requested=str(root.root))
    _write_skill(root.skills_store, "code-review")
    await _registry(config, thread_store).services_for(record={}, thread_id=None, requested=str(root.root))
    backend = build_backend(config, store, scope=root)

    downloaded = backend.download_files([f"{VIRTUAL_SKILL_VIEW}/code-review/SKILL.md"])

    assert downloaded[0].error is None
    assert b"code-review" in (downloaded[0].content or b"")
    assert backend.write(f"{VIRTUAL_SKILLS}/code-review/SKILL.md", "改掉").error is not None


async def test_tool_outputs_are_readable_by_their_virtual_path(
    tmp_path: Path, store: BaseStore
) -> None:
    """工具留存：宿主落盘路径与消息里那个虚拟路径必须指向同一个文件。

    WHY 端到端断言「两半同源」：落盘由服务端用宿主路径做、引用写进消息与事件，而回取可能
    走 Agent 的挂载、也可能走文件面板的解析（``resolve_in_workspace``）。这三条路必须落到
    同一个文件上——否则表现是前端点开「完整输出」时 404，看起来像留存没写成功。
    """
    config = make_config(tmp_path)
    root = make_root(config)
    root.ensure_storage()
    path = tool_output_path(root.tool_output_store, "t1", 1, "execute")
    write_tool_output(path, "完整输出" * 100, max_chars=10_000)
    virtual = tool_output_virtual_path(root.tool_outputs_virtual, "t1", path.name)
    backend = build_backend(config, store, scope=root)

    via_mount = backend.download_files([virtual])[0]
    via_panel = resolve_in_workspace(root.root, virtual, mounts=root.mount_table)

    assert via_mount.error is None
    assert "完整输出" in (via_mount.content or b"").decode("utf-8")
    assert via_panel == path
