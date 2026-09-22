"""场景预设与工作空间的绑定：解析、锁定与持久化。

WHY 需要这一组用例：场景决定技能视图里放哪些技能，而视图**按工作区物化**
（``<工作区>/.harness/skills-active``）且被图缓存持有——所以「一个工作空间只属于一个场景」
是这套设计的物理约束，不是产品偏好。它一旦失效，两条会话会各自按不同场景重建视图、互相
覆盖，而两侧都不报错：先跑的那条仍按自己首轮加载的技能索引做事（技能索引每会话只加载
一次，错了不会自愈）。

这里钉住四件事：场景随根解析出来、冲突被拒、允许补选、以及它真的落库且不可改写。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application.errors import SessionPresetLockedError
from config import SessionRoot
from runtime.thread_store import ThreadMetaStore
from tests.application.test_session_registry import _registry
from tests.conftest import make_config, make_root


# ------------------------------------------------------------------ 解析：场景随根走


async def test_resolve_carries_the_locked_preset_into_the_root(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """库里的场景必须被带进 ``SessionRoot``——否则技能视图会按「不限定」重建。"""
    config = make_config(tmp_path)
    workspace = make_root(config, name="project-a")
    registry = _registry(config, thread_store)
    record = {"workspace": str(workspace.root), "workspace_bound": True, "preset": "coding"}

    root = await registry.resolve(thread_id="t1", record=record)

    assert root.preset == "coding"


async def test_a_fresh_session_uses_the_requested_preset(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """首轮交互时给出的场景要落到根上（此刻还没有库记录）。"""
    config = make_config(tmp_path)
    workspace = make_root(config, name="project-a")
    registry = _registry(config, thread_store)

    root = await registry.resolve(
        thread_id="t1",
        requested=str(workspace.root),
        allow_missing=True,
        preset="coding",
    )

    assert root.preset == "coding"


async def test_without_a_preset_the_root_is_unrestricted(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """不选场景是正常路径：根上的场景为空串，过滤器按「不限定」处理。"""
    config = make_config(tmp_path)
    workspace = make_root(config, name="project-a")
    registry = _registry(config, thread_store)

    root = await registry.resolve(thread_id="t1", requested=str(workspace.root), allow_missing=True)

    assert root.preset == ""


# ------------------------------------------------------------------ 锁定与补选


async def test_a_conflicting_preset_is_rejected(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """同一工作空间换场景必须被拒——视图按根一份，两个场景会互相覆盖。"""
    config = make_config(tmp_path)
    workspace = make_root(config, name="project-a")
    registry = _registry(config, thread_store)
    record = {"workspace": str(workspace.root), "workspace_bound": True, "preset": "coding"}

    with pytest.raises(SessionPresetLockedError):
        await registry.resolve(thread_id="t1", record=record, preset="writing")


async def test_an_empty_preset_can_be_filled_in_later(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """首轮没选场景的会话允许之后补选。

    WHY 与文件根的严格锁定不同：场景只决定技能集，不会让已有产物失联；而"选工作空间时忘了
    选场景"是很常见的操作顺序，为此要求用户重建会话并不合理。
    """
    config = make_config(tmp_path)
    workspace = make_root(config, name="project-a")
    registry = _registry(config, thread_store)
    record = {"workspace": str(workspace.root), "workspace_bound": True, "preset": ""}

    root = await registry.resolve(thread_id="t1", record=record, preset="coding")

    assert root.preset == "coding"


async def test_the_same_preset_is_accepted(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """重复给出同一个场景不算冲突——它是幂等的（客户端每轮都带同一个值很常见）。"""
    config = make_config(tmp_path)
    workspace = make_root(config, name="project-a")
    registry = _registry(config, thread_store)
    record = {"workspace": str(workspace.root), "workspace_bound": True, "preset": "coding"}

    root = await registry.resolve(thread_id="t1", record=record, preset="coding")

    assert root.preset == "coding"


# ------------------------------------------------------------------ 持久化


async def test_record_turn_persists_the_preset_once(tmp_path: Path) -> None:
    """场景只在尚未记录时写入，之后不再改写（与文件根同一条 UPSERT 的语义）。"""
    async with _open(tmp_path) as store:
        await store.create("a" * 32, workspace=str(tmp_path), workspace_bound=True)
        await store.record_turn("a" * 32, title_hint="你好", preset="coding")
        after_first = await store.get("a" * 32)
        await store.record_turn("a" * 32, title_hint=None, turn_delta=0, preset="writing")
        after_second = await store.get("a" * 32)

    assert after_first is not None and after_first["preset"] == "coding"
    assert after_second is not None and after_second["preset"] == "coding"


async def test_create_records_the_preset(tmp_path: Path) -> None:
    async with _open(tmp_path) as store:
        record = await store.create("b" * 32, preset="coding")

    assert record["preset"] == "coding"


async def test_an_invalid_preset_is_rejected_by_the_store(tmp_path: Path) -> None:
    """存储层与预设加载用同一套规则：非法 ID 不能落库，否则库里会存着匹配不上目录的值。"""
    async with _open(tmp_path) as store:
        with pytest.raises(ValueError):
            await store.create("c" * 32, preset="Coding")
        with pytest.raises(ValueError):
            await store.create("d" * 32, preset="coding-")


# ------------------------------------------------------------------ 不变量


def test_the_root_repr_mentions_the_preset(tmp_path: Path) -> None:
    """排障时要能一眼看出这条根属于哪个场景——日志里只有路径的话，场景问题无从发现。"""
    config = make_config(tmp_path)
    root = SessionRoot(config, config.db_path.parent / "ws", "coding")

    assert "preset=coding" in repr(root)


def test_root_normalizes_the_preset(tmp_path: Path) -> None:
    """前后空格在解析期就被去掉：否则它会成为「永远匹配不上」的隐形差异。"""
    config = make_config(tmp_path)
    root = SessionRoot(config, config.db_path.parent / "ws", " coding ")

    assert root.preset == "coding"


# --------------------------------------------------- 装配：技能视图与场景对齐


def _write_preset(tmp_path: Path, preset_id: str, skill: str) -> Path:
    """写一个「场景 + 它专属技能包」的预设目录，返回预设根目录。"""
    presets = tmp_path / "presets"
    package = presets / preset_id / skill
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: {skill} 的说明\n---\n\n# {skill}\n", encoding="utf-8"
    )
    (presets / preset_id / "preset.toml").write_text(
        f'title = "{preset_id}"\nskills = ["{skill}"]\n', encoding="utf-8"
    )
    return presets


def _view_names(root: SessionRoot) -> set[str]:
    """技能视图里当前有哪些技能（视图就在 ``.harness/skills-active`` 下）。"""
    view = root.skill_view_store
    return {child.name for child in view.iterdir() if child.is_dir()} if view.is_dir() else set()


async def test_a_provisional_assembly_is_rebuilt_when_the_scenario_arrives(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """场景还没定下来时装配过一次，之后带场景再要 → 重建视图，而不是报冲突。

    WHY 这条是实测撞出来的：首条消息的附件解析先按草稿根装配（那时场景还是空），随后面板按
    已锁定的场景再请求同一个工作空间。把先那次当成既定事实，用户什么都没做错却收到 409；
    而只「复用」更糟——场景技能永远进不了视图（面板显示一套、Agent 用另一套，且都不报错）。
    """
    presets = _write_preset(tmp_path, "coding", "commit-message")
    config = make_config(tmp_path, presets_dir=presets)
    workspace = make_root(config, name="project-a")
    registry = _registry(config, thread_store)
    provisional = await registry.services(SessionRoot(config, workspace.root, ""))

    assert "commit-message" not in _view_names(provisional.root), "不限定 → 不该有场景技能"

    bound = await registry.services(SessionRoot(config, workspace.root, "coding"))

    assert bound is not provisional, "场景由空变具体必须重建，不能沿用那次临时装配"
    assert bound.root.preset == "coding"
    assert "commit-message" in _view_names(bound.root), "重建后视图必须含场景技能"


async def test_an_unrestricted_request_reuses_the_bound_scenario(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """库里已有具体场景、而本次请求没给 → 复用那份：既不报错、也不重建。

    WHY 复用而不是按「不限定」重建：视图已经是那个场景的，重建会把正在用它的会话的技能集
    换掉（技能索引每会话只加载一次，换了不报错、只是行为变了）；而那个场景仍是这个工作空间
    的事实，面板也应如实展示它。
    """
    presets = _write_preset(tmp_path, "coding", "commit-message")
    config = make_config(tmp_path, presets_dir=presets)
    workspace = make_root(config, name="project-a")
    registry = _registry(config, thread_store)
    bound = await registry.services(SessionRoot(config, workspace.root, "coding"))

    reused = await registry.services(SessionRoot(config, workspace.root, ""))

    assert reused is bound
    assert reused.root.preset == "coding"


async def test_two_concrete_scenarios_on_one_workspace_are_rejected(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """两边都是具体场景且不同 → 拒绝（视图按根一份，重建等于换掉别人的技能集）。"""
    presets = _write_preset(tmp_path, "coding", "commit-message")
    _write_preset(tmp_path, "writing", "markdown-style")
    config = make_config(tmp_path, presets_dir=presets)
    workspace = make_root(config, name="project-a")
    registry = _registry(config, thread_store)
    await registry.services(SessionRoot(config, workspace.root, "coding"))

    with pytest.raises(SessionPresetLockedError):
        await registry.services(SessionRoot(config, workspace.root, "writing"))


def _open(tmp_path: Path):
    """打开一个临时的会话元数据存储。"""
    from runtime.thread_store import open_thread_store

    return open_thread_store(tmp_path / "threads.db")
