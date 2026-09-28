"""工作区内的应用数据目录（``.harness/``）：位置、迁移与只读挂载。

WHY 需要这一组用例：2026-09-22 把技能库 / 技能视图 / 工具留存 / 知识库索引从
``<数据目录>/roots/<根标识>/`` 搬回了**工作区内**的 ``.harness/``——理由是这些内容与
工作空间强绑定（技能是「这个项目常用的套路」，留存是「这个项目的运行记录」），跟着项目走
才能让换机器、换工作空间之后行为一致。

这次调整的验收点因此有三条，且**任何一条回退都不会报错**：

1. 位置：全部落在 ``<工作区>/.harness/`` 下，且只有一个入口名字（不是散开的三四个）；
2. 迁移：两代旧位置（``<数据目录>/roots/<根标识>/`` 与更早的 ``<工作区>/skills/``）里的
   技能包与留存都要搬过来——技能是用户放进来的东西，静默丢掉等于让它凭空消失；
3. 只读：内容虽然在工作区内，但 Agent **改不动**（``/skills``、``/.harness/`` 等只读路由），
   否则「Agent 能重写自己的技能库」这件事会以「行为忽然变了」的形式出现。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from langgraph.store.base import BaseStore

from agent.backends import build_backend
from config import (
    HARNESS_DIR_NAME,
    VIRTUAL_HARNESS,
    VIRTUAL_SKILLS,
    VIRTUAL_SKILL_VIEW,
    VIRTUAL_TOOL_OUTPUTS,
    SessionRoot,
)
from runtime.store import open_store
from runtime.thread_store import ThreadMetaStore
from runtime.tool_outputs import tool_output_path, tool_output_virtual_path, write_tool_output
from runtime.workspace_files import resolve_in_workspace
from tests.application.test_session_registry import _registry
from tests.conftest import make_config, make_root

_SKILL = "---\nname: {name}\ndescription: {name} 的说明\n---\n\n# {name}\n"

_STORED_NAMES = ("skills", "tool-outputs")
"""``.harness/`` 下由 ``ensure_storage`` 直接建出来的两个子目录名。"""


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


def test_storage_dir_lives_inside_the_workspace(tmp_path: Path) -> None:
    """存储目录固定在工作区内的 ``.harness/``：同一个根恒等，不同根各有一份。"""
    config = make_config(tmp_path)
    first = make_root(config, name="project-a")
    again = SessionRoot(config, first.root)
    other = make_root(config, name="project-b")

    assert first.storage_dir == again.storage_dir
    assert first.storage_dir == first.root / HARNESS_DIR_NAME
    assert other.storage_dir == other.root / HARNESS_DIR_NAME
    assert first.storage_dir != other.storage_dir


def test_the_storage_dir_follows_the_workspace_not_the_data_directory(tmp_path: Path) -> None:
    """同一个工作区在不同数据目录下得到同一个存储目录（存储跟着项目走）。

    WHY 反向断言（这是本次调整的核心）：旧布局按数据目录派生存储位置，于是「换个数据目录
    或换台机器」会让技能库看起来消失；现在它只取决于工作区。
    """
    workspace = tmp_path / "project"
    workspace.mkdir()
    left = SessionRoot(make_config(tmp_path / "left"), workspace)
    right = SessionRoot(make_config(tmp_path / "right"), workspace)

    assert left.storage_dir == right.storage_dir == workspace / HARNESS_DIR_NAME


def test_every_store_lives_under_the_single_harness_directory(tmp_path: Path) -> None:
    """技能库 / 技能视图 / 工具留存 / 知识库索引都在 ``.harness/`` 下。

    WHY 单列：它们曾分散在数据目录里（甚至分散到 ``roots/<根标识>/``），改回工作区时最
    容易出现的偏差是「一半搬了、一半没搬」——而那种偏差不会报错，只表现为用户在某处找
    不到自己的东西。
    """
    root = make_root(make_config(tmp_path))

    assert root.skills_store == root.storage_dir / "skills"
    assert root.skill_view_store == root.storage_dir / "skills-active"
    assert root.tool_output_store == root.storage_dir / "tool-outputs"
    assert root.knowledge_db == root.storage_dir / "knowledge.db"
    assert root.storage_dir.parent == root.root


def test_ensure_storage_creates_the_stores_but_not_the_view(tmp_path: Path) -> None:
    """建技能库与留存目录，但**不**预建技能视图目录。

    WHY 视图目录不能预建：``sources_for_graph`` 用「视图目录是否存在」判断「物化视图是否
    建过」——预建一个空目录会让那个判据永远为真，于是「视图被清理 / 重建失败」与「所有
    技能都停用」再也分不开（前者会让技能静默消失且没有告警）。
    """
    root = make_root(make_config(tmp_path))

    root.ensure_storage()

    assert _names(root.storage_dir) == set(_STORED_NAMES)
    assert root.skill_view_store.exists() is False


# ------------------------------------------------------------------ 装配后的工作区形态


async def test_assembling_a_root_adds_the_harness_directory(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """装配一个根之后，工作区里除了用户文件就多一个 ``.harness/``。

    WHY 这是本次调整的验收点（2026-09-22）：应用数据回到工作区内是**有意**的取舍（要随
    项目走），但必须只有一个名字、且内容都能在它下面找到——否则「应用往我的项目里写了
    什么」就说不清了。
    """
    config = make_config(tmp_path)
    root = make_root(config)
    (root.root / "README.md").write_text("用户自己的文件\n", encoding="utf-8")

    await _registry(config, thread_store).services_for(
        record={}, thread_id=None, requested=str(root.root)
    )

    assert _names(root.root) == {"README.md", HARNESS_DIR_NAME}, "工作区里只该多出 .harness"
    assert _names(root.storage_dir) >= set(_STORED_NAMES)


async def test_the_skill_view_lands_under_the_harness_directory(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """装配会建出技能视图——它落在 ``.harness/`` 下，而不是工作区根下。"""
    config = make_config(tmp_path)
    root = make_root(config)

    await _registry(config, thread_store).services_for(
        record={}, thread_id=None, requested=str(root.root)
    )

    assert root.skill_view_store.is_dir()
    assert root.skill_view_store.parent == root.storage_dir
    assert not (root.root / ".skills-active").exists()


# ------------------------------------------------------------------ 迁移：更早的 <工作区>/skills


async def test_legacy_skill_library_is_migrated_into_the_harness(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """工作区根下的 ``skills/``（更早的布局）里的技能包要搬进 ``.harness/skills``。"""
    config = make_config(tmp_path)
    root = make_root(config)
    package = _write_skill(root.root / "skills", "code-review")

    await _registry(config, thread_store).services_for(
        record={}, thread_id=None, requested=str(root.root)
    )

    assert (root.skills_store / "code-review" / "SKILL.md").is_file()
    assert not package.exists()
    assert not (root.root / "skills").exists()


async def test_a_same_named_directory_without_skill_packages_is_left_alone(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """工作区里恰好叫 ``skills`` 的**普通**目录不动它。

    WHY 单列（这是一条用户数据保护线）：``skills`` 是很常见的目录名，历史上出现过「打开
    项目后应用把用户的目录搬走」——用户找不到自己的东西，而全程没有任何提示。因此只有
    「含 ``SKILL.md`` 的子目录」才被当成技能库。
    """
    config = make_config(tmp_path)
    root = make_root(config)
    ordinary = root.root / "skills"
    ordinary.mkdir()
    (ordinary / "README.md").write_text("这不是技能包\n", encoding="utf-8")

    await _registry(config, thread_store).services_for(
        record={}, thread_id=None, requested=str(root.root)
    )

    assert (ordinary / "README.md").is_file(), "不含技能包的 skills/ 必须原样保留"


async def test_an_empty_legacy_skill_library_is_left_alone(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """空的 ``skills/`` 保持原样——它也可能是用户自己刚建的目录。"""
    config = make_config(tmp_path)
    root = make_root(config)
    (root.root / "skills").mkdir()

    await _registry(config, thread_store).services_for(
        record={}, thread_id=None, requested=str(root.root)
    )

    assert (root.root / "skills").is_dir()


async def test_configured_skill_dirs_are_left_alone(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """``SKILL_DIRS`` 显式指向工作区里的目录时：那是**用户配置的**路径，不能当旧位置搬走。

    WHY 单列（实测踩到）：迁移逻辑按「``<根>/skills`` 存在且像技能库」判断，而用户完全
    可能正是把技能目录配在那儿——搬走会让配置里的路径凭空消失，症状是「技能一个都列不出来」。
    """
    config = make_config(tmp_path)
    root = make_root(config)
    package = _write_skill(root.root / "skills", "code-review")
    configured = make_config(tmp_path, skill_dirs=[root.root / "skills"])
    registry = _registry(configured, thread_store)

    await registry.services_for(record={}, thread_id=None, requested=str(root.root))

    assert package.is_dir(), "显式配置的技能目录不该被搬走"
    assert not (root.storage_dir / "skills").exists(), "显式配置时不用我们那份技能库"


# ------------------------------------------------------------------ 迁移：旧版根外存储


async def test_the_old_root_store_is_migrated_into_the_workspace(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """旧版根外存储（``<数据目录>/roots/<根标识>/``）里的技能库与留存要搬进 ``.harness/``。

    WHY 单列：不搬等于「升级一次，用户的技能库与历史留存凭空消失」，而且没有任何报错——
    只有日志里少了一行 INFO。
    """
    config = make_config(tmp_path)
    root = make_root(config)
    legacy = root.legacy_root_store_dir
    _write_skill(legacy / "skills", "code-review")
    legacy_tool = legacy / "tool-outputs" / "t1"
    legacy_tool.mkdir(parents=True)
    (legacy_tool / "0001-execute.txt").write_text("旧留存\n", encoding="utf-8")

    await _registry(config, thread_store).services_for(
        record={}, thread_id=None, requested=str(root.root)
    )

    assert (root.skills_store / "code-review" / "SKILL.md").is_file()
    assert (root.tool_output_store / "t1" / "0001-execute.txt").is_file()
    assert not legacy.exists(), "旧存储搬空后应被顺手清掉"


async def test_a_non_empty_new_store_is_not_overwritten_by_the_old_one(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """新位置已有技能库时，旧位置原样保留、只告警——不做静默合并。

    WHY：合并两份技能库没有任何「正确」的规则（同名技能谁赢？），而静默覆盖会让用户以为
    自己的改动丢了。两边都留着，由用户决定。
    """
    config = make_config(tmp_path)
    root = make_root(config)
    root.ensure_storage()
    _write_skill(root.skills_store, "new-one")
    legacy = root.legacy_root_store_dir
    _write_skill(legacy / "skills", "old-one")

    await _registry(config, thread_store).services_for(
        record={}, thread_id=None, requested=str(root.root)
    )

    assert (root.skills_store / "new-one" / "SKILL.md").is_file()
    assert not (root.skills_store / "old-one").exists(), "不合并：新位置不该多出旧技能"
    assert (legacy / "skills" / "old-one" / "SKILL.md").is_file(), "旧位置必须原样保留"


# ------------------------------------------------------------------ 挂载与只读


def test_mount_table_covers_the_virtual_paths(tmp_path: Path) -> None:
    """四个虚拟路径都挂在表上，并指向工作区内的应用数据目录。"""
    root = make_root(make_config(tmp_path))

    table = root.mount_table

    assert table[f"{VIRTUAL_HARNESS}/"] == root.storage_dir
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
    await _registry(config, thread_store).services_for(
        record={}, thread_id=None, requested=str(root.root)
    )
    _write_skill(root.skills_store, "code-review")
    await _registry(config, thread_store).services_for(
        record={}, thread_id=None, requested=str(root.root)
    )
    backend = build_backend(config, store, scope=root)

    downloaded = backend.download_files([f"{VIRTUAL_SKILL_VIEW}/code-review/SKILL.md"])

    assert downloaded[0].error is None
    assert b"code-review" in (downloaded[0].content or b"")
    assert backend.write(f"{VIRTUAL_SKILLS}/code-review/SKILL.md", "改掉").error is not None


async def test_the_backend_refuses_writes_through_the_harness_path(
    tmp_path: Path, store: BaseStore, thread_store: ThreadMetaStore
) -> None:
    """Agent 也不能经 ``/.harness/...`` 改写自己的技能库。

    WHY 单列（这是本次调整新增的绕行口）：应用数据现在物理上就在工作区内，而 ``/skills``
    只覆盖它自己的前缀——没有 ``/.harness/`` 这条只读路由的话，Agent 换一条路径就能写到
    同一批文件（``virtual_mode`` 只拦越界，不拦写）。
    """
    config = make_config(tmp_path)
    root = make_root(config)
    await _registry(config, thread_store).services_for(
        record={}, thread_id=None, requested=str(root.root)
    )
    _write_skill(root.skills_store, "code-review")
    backend = build_backend(config, store, scope=root)

    assert backend.write(f"{VIRTUAL_HARNESS}/skills/code-review/SKILL.md", "改掉").error is not None
    assert (root.skills_store / "code-review" / "SKILL.md").read_text(encoding="utf-8").count(
        "改掉"
    ) == 0


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
