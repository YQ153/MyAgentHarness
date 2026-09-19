"""技能启停状态的持久化。

重点覆盖三处：

1. **「没有记录」与「记录为启用」是两回事**：前者是默认，后者是用户做过的选择。两者
   当前行为一致，但混为一谈后，将来改默认值就会悄悄改掉用户已表达过的选择。
2. **作用域隔离**：现在只有 ``global``，但隔离必须在表结构上就成立——后续加场景时是新增
   取值，而不是回头补一列。
3. **真的落库**：关掉连接再打开，状态还在。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from runtime.skill_store import DEFAULT_ENABLED, GLOBAL_SCOPE, SkillStateStore, open_skill_store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SkillStateStore]:
    """临时目录里的技能状态库。"""
    async with open_skill_store(tmp_path / "skills.db") as opened:
        yield opened


# --------------------------------------------------------------- 默认语义


async def test_unknown_skill_defaults_to_enabled(store: SkillStateStore) -> None:
    """没有记录即启用——技能包是用户主动放进来的，放进来的下一刻就期望能用。"""
    assert DEFAULT_ENABLED is True
    assert await store.is_enabled("code-review") is True


async def test_default_scope_is_global(store: SkillStateStore) -> None:
    """不传 scope 时落在全局作用域。"""
    await store.set_enabled("code-review", False)

    assert await store.is_enabled("code-review", scope=GLOBAL_SCOPE) is False


async def test_enabled_map_contains_only_recorded_skills(store: SkillStateStore) -> None:
    """``enabled_map`` 只返回被显式设置过的技能——它不替调用方补默认值。"""
    await store.set_enabled("code-review", False)

    assert await store.enabled_map() == {"code-review": False}


async def test_resolve_fills_defaults_for_the_whole_list(store: SkillStateStore) -> None:
    """``resolve`` 一次给出整份清单的状态（含默认）。"""
    await store.set_enabled("legacy", False)

    resolved = await store.resolve(["code-review", "legacy", "doc-to-markdown"])

    assert resolved == {"code-review": True, "legacy": False, "doc-to-markdown": True}


async def test_disabled_names_lists_only_disabled(store: SkillStateStore) -> None:
    """``disabled_names`` 只列被停用的。"""
    await store.set_enabled("legacy", False)
    await store.set_enabled("code-review", True)

    assert await store.disabled_names() == {"legacy"}


# --------------------------------------------------------------- 写入


async def test_explicit_enable_is_recorded_not_deleted(store: SkillStateStore) -> None:
    """显式启用会留下记录，而不是把行删掉。

    WHY：这样「用户在这里做过选择」是可查的。代价是一行与默认值重复的记录，比
    「查不到任何痕迹」更容易解释。
    """
    await store.set_enabled("code-review", True)

    assert await store.enabled_map() == {"code-review": True}


async def test_set_enabled_upserts(store: SkillStateStore) -> None:
    """重复设置是覆盖，不产生第二行。"""
    await store.set_enabled("code-review", False)
    await store.set_enabled("code-review", True)

    assert await store.enabled_map() == {"code-review": True}


async def test_set_enabled_returns_record_with_timestamp(store: SkillStateStore) -> None:
    """返回值带上写入时刻，便于调用方回显。"""
    record = await store.set_enabled("code-review", False)

    assert record["skill_name"] == "code-review"
    assert record["enabled"] is False
    assert record["updated_at"]


async def test_forget_returns_to_default(store: SkillStateStore) -> None:
    """``forget`` 清掉记录，技能回到默认启用。"""
    await store.set_enabled("code-review", False)

    assert await store.forget("code-review") is True
    assert await store.is_enabled("code-review") is True
    assert await store.enabled_map() == {}


async def test_forget_missing_record_returns_false(store: SkillStateStore) -> None:
    """清一个本来就没有的记录返回 False，而不是报错。"""
    assert await store.forget("nope") is False


# --------------------------------------------------------------- 作用域


async def test_scopes_are_isolated(store: SkillStateStore) -> None:
    """一个作用域的停用不影响另一个。"""
    await store.set_enabled("code-review", False, scope="scenario:demo")

    assert await store.is_enabled("code-review", scope="scenario:demo") is False
    assert await store.is_enabled("code-review") is True


# --------------------------------------------------------------- 校验


async def test_invalid_arguments_are_rejected(store: SkillStateStore) -> None:
    """空值、超长与非布尔的 enabled 都在入口拦下。"""
    with pytest.raises(ValueError, match="scope 不能为空"):
        await store.enabled_map(scope="   ")
    with pytest.raises(ValueError, match="skill_name 不能为空"):
        await store.is_enabled("  ")
    with pytest.raises(ValueError, match="skill_name 过长"):
        await store.set_enabled("n" * 65, False)
    with pytest.raises(ValueError, match="scope 过长"):
        await store.set_enabled("code-review", False, scope="s" * 65)


@pytest.mark.parametrize("bad", [1, 0, "false", None])
async def test_enabled_must_be_a_real_boolean(store: SkillStateStore, bad: object) -> None:
    """``enabled`` 必须是布尔。

    WHY 单独测：``bool`` 是 ``int`` 的子类，1 / 0 混进来会让「传错了变量」一路通过，
    直到某次把字符串 ``"false"`` 当成真值用。
    """
    with pytest.raises(ValueError, match="布尔"):
        await store.set_enabled("code-review", bad)  # type: ignore[arg-type]


# --------------------------------------------------------------- 持久化


async def test_state_survives_reopen(tmp_path: Path) -> None:
    """关掉连接再打开，状态还在——这是「落库」与「记在内存里」的区别。"""
    path = tmp_path / "skills.db"
    async with open_skill_store(path) as first:
        await first.set_enabled("legacy", False)

    async with open_skill_store(path) as second:
        assert await second.is_enabled("legacy") is False


async def test_ping_reports_connection_usable(store: SkillStateStore) -> None:
    """连通性探测可用。"""
    assert await store.ping() is True
