"""消息编辑 / 重新生成 / 分叉的回归测试。

覆盖面：分叉点的解析（尤其是「不许挑到别的分支上」）、分支登记与当前分支游标、
旧分支被冻结、运行中拒绝分叉、只有用户消息可编辑、没有用户消息时不可重生成。

WHY 用假图而不是真实 LangGraph 图：这里要断言的是**我们自己的**分支记账与分叉点
解析——上游「按 checkpoint_id 能分叉」这件事已由 ``scripts/probe_checkpoint_fork.py``
用真检查点验证过一次，在单元测试里再跑一遍只会让失败指向上游而不是指向我们。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from application.errors import NotFoundError, ThreadBusyError
from application.run_service import RunService
from application.thread_service import ThreadService
from tests.application.test_run_service import (
    FakeGraphFactory,
    _drain,
    _make_service,
)


class Snapshot:
    """一条检查点快照替身：形状与 LangGraph 的 StateSnapshot 对齐。"""

    def __init__(self, checkpoint_id: str, messages: list[Any]) -> None:
        self.config = {
            "configurable": {"thread_id": "t1", "checkpoint_id": checkpoint_id}
        }
        self.values = {"messages": messages}


class BranchingGraph:
    """支持「按检查点读状态」与「枚举历史」的图替身。

    历史按**新到旧**给出，与上游的 ``aget_state_history`` 顺序一致——分叉点的解析
    逻辑依赖这个顺序，替身若反过来，测试就会在替身上通过、在上游失败。
    """

    def __init__(self, snapshots: list[Snapshot]) -> None:
        if not snapshots:
            raise ValueError("至少需要一条快照")
        self._snapshots = snapshots
        self.astream_configs: list[dict[str, Any]] = []

    async def aget_state(self, config: dict[str, Any]) -> Snapshot:
        wanted = (config.get("configurable") or {}).get("checkpoint_id")
        if not wanted:
            return self._snapshots[0]
        for snapshot in self._snapshots:
            if snapshot.config["configurable"]["checkpoint_id"] == wanted:
                return snapshot
        raise ValueError(f"未知检查点：{wanted}")

    async def aget_state_history(
        self, config: dict[str, Any]
    ) -> AsyncIterator[Snapshot]:
        for snapshot in self._snapshots:
            yield snapshot

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[Any]:
        self.astream_configs.append(config or {})
        return
        yield  # noqa: WPS328 不可达，仅为构造异步生成器


def _turn(
    index: int, answer: str = "", *, override: str | None = None
) -> list[Any]:
    """构造第 ``index`` 轮（用户 + 助手）两条消息。"""
    asked = override if override is not None else f"第{index}轮"
    return [
        HumanMessage(content=asked, id=f"h{index}"),
        AIMessage(content=answer or f"回复<{asked}>", id=f"a{index}"),
    ]


def _chain(turns: int = 2) -> list[Snapshot]:
    """构造一段 n 轮会话的检查点链，按**新到旧**返回。

    ``cp-k`` 表示「第 k 轮结束之后」的状态，``cp-0`` 是第 1 轮之前的空状态；
    列表首元是头部（最新），与上游 ``aget_state_history`` 的顺序一致。
    """
    states: list[list[Any]] = [[]]
    messages: list[Any] = []
    for step in range(1, turns + 1):
        messages = messages + _turn(step)
        states.append(list(messages))
    return [Snapshot(f"cp-{step}", states[step]) for step in range(turns, -1, -1)]


def _async_service(config: Any, store: Any, graph: Any) -> RunService:
    return _make_service(config, store, graph)


def _thread_service(config: Any, store: Any, graph: Any) -> ThreadService:
    """构造 ThreadService；checkpointer 只被构造时的空值校验读到，给个占位对象即可。

    WHY 不复用真实检查点：读历史与切分支这条路只经 ``graph.aget_state``，检查点侧
    在本文件里没有任何调用点——引入真实保存器只会让每个用例都多一次建库开销。
    """
    return ThreadService(
        config,
        checkpointer=object(),
        thread_store=store,
        graph_factory=FakeGraphFactory(graph),
    )


# ------------------------------------------------------------------ 分叉点


async def test_regenerate_forks_before_the_last_user_message(test_config, thread_store):
    graph = BranchingGraph(_chain(2))
    service = _async_service(test_config, thread_store, graph)
    await thread_store.create("t1", title="会话")

    await _drain(await service.regenerate("t1"))

    # 头是「两轮之后」，故分叉点应当是「第一轮之后」那个检查点
    assert graph.astream_configs[0]["configurable"]["checkpoint_id"] == "cp-1"


async def test_edit_forks_before_the_target_message(test_config, thread_store):
    graph = BranchingGraph(_chain(2))
    service = _async_service(test_config, thread_store, graph)
    await thread_store.create("t1", title="会话")

    await _drain(await service.edit("t1", 0, "改过的第一问"))

    # 下标 0 是第一条用户消息，它之前的状态就是空状态
    assert graph.astream_configs[0]["configurable"]["checkpoint_id"] == "cp-0"


async def test_fork_point_skips_history_from_other_branches(test_config, thread_store):
    """其它分支上「条数恰好相同」的历史不得被选中。

    WHY 这是本实现最危险的一处：``aget_state_history`` 给出的是整个会话的检查点，
    其中混着别的分支。若只按消息条数挑，会挑到另一条分支上——后果是用户拿到一段
    自己没写过的上下文，而且没有任何报错，只能靠人眼发现。
    """
    head = _turn(1) + _turn(2)
    decoy = [
        HumanMessage(content="别处的问", id="other-h"),
        AIMessage(content="别处的答", id="other-a"),
    ]
    graph = BranchingGraph(
        [
            Snapshot("cp-head", head),
            Snapshot("cp-decoy", decoy),  # 条数与「第一轮之后」相同，但 id 不同
            Snapshot("cp-1", _turn(1)),
            Snapshot("cp-0", []),
        ]
    )
    service = _async_service(test_config, thread_store, graph)
    await thread_store.create("t1", title="会话")

    await _drain(await service.regenerate("t1"))

    assert graph.astream_configs[0]["configurable"]["checkpoint_id"] == "cp-1"


# ------------------------------------------------------------------ 分支记账


async def test_regenerate_freezes_old_branch_and_activates_new(test_config, thread_store):
    graph = BranchingGraph(_chain(2))
    service = _async_service(test_config, thread_store, graph)
    await thread_store.create("t1", title="会话")

    await _drain(await service.regenerate("t1"))

    record = await thread_store.get("t1")
    current = record["current_branch"]
    branches = {row["branch_id"]: row for row in await thread_store.list_branches("t1")}

    assert current != ""
    # 旧分支（根）的头被冻结在离开时的位置——否则它下次被切回来会接错地方
    assert branches[""]["head_checkpoint"] == "cp-2"
    assert branches[current]["parent_branch_id"] == ""
    assert branches[current]["origin"] == "regenerate"
    assert branches[current]["head_checkpoint"] == ""


async def test_edit_labels_the_branch_with_the_turn_number(test_config, thread_store):
    graph = BranchingGraph(_chain(2))
    service = _async_service(test_config, thread_store, graph)
    await thread_store.create("t1", title="会话")

    await _drain(await service.edit("t1", 2, "改过的第二问"))

    current = (await thread_store.get("t1"))["current_branch"]
    branches = {row["branch_id"]: row for row in await thread_store.list_branches("t1")}
    assert branches[current]["origin"] == "edit"
    assert branches[current]["label"] == "编辑第 2 轮"


async def test_activate_branch_freezes_the_branch_being_left(test_config, thread_store):
    graph = BranchingGraph(_chain(2))
    runs = _async_service(test_config, thread_store, graph)
    threads = _thread_service(test_config, thread_store, graph)
    await thread_store.create("t1", title="会话")

    await _drain(await runs.regenerate("t1"))
    left = (await thread_store.get("t1"))["current_branch"]

    result = await threads.activate_branch("t1", "")

    assert result.current_branch == ""
    branches = {row["branch_id"]: row for row in await thread_store.list_branches("t1")}
    assert branches[left]["head_checkpoint"] == "cp-2"


async def test_branch_list_includes_root_before_any_fork(test_config, thread_store):
    threads = _thread_service(test_config, thread_store, BranchingGraph(_chain(1)))
    await thread_store.create("t1", title="会话")

    result = await threads.list_branches("t1")

    assert result.current_branch == ""
    assert len(result.items) == 1
    assert result.items[0].branch_id == ""
    assert result.items[0].current is True


# ------------------------------------------------------------------ 拒绝路径


async def test_edit_rejects_non_user_message(test_config, thread_store):
    service = _async_service(test_config, thread_store, BranchingGraph(_chain(2)))
    await thread_store.create("t1", title="会话")

    with pytest.raises(ValueError, match="只能编辑用户消息"):
        await service.edit("t1", 1, "不该成功")


async def test_edit_rejects_out_of_range_index(test_config, thread_store):
    service = _async_service(test_config, thread_store, BranchingGraph(_chain(2)))
    await thread_store.create("t1", title="会话")

    with pytest.raises(ValueError, match="越界"):
        await service.edit("t1", 99, "不该成功")


async def test_regenerate_without_user_message_fails(test_config, thread_store):
    service = _async_service(
        test_config, thread_store, BranchingGraph([Snapshot("cp-0", [])])
    )
    await thread_store.create("t1", title="会话")

    with pytest.raises(ValueError, match="还没有用户消息"):
        await service.regenerate("t1")


async def test_edit_rejected_while_running(test_config, thread_store):
    """运行中必须拒绝分叉，且要在事件流开始之前就失败。"""
    graph = BranchingGraph(_chain(2))
    service = _async_service(test_config, thread_store, graph)
    await thread_store.create("t1", title="会话")
    service._acquire_run_slot("t1")  # noqa: SLF001 - 测试直接占住槽位

    with pytest.raises(ThreadBusyError):
        await service.edit("t1", 0, "不该成功")


# ------------------------------------------------------------------ 按分支读历史


async def test_history_reads_the_selected_branch(test_config, thread_store):
    """切到旧分支后读到的应当是**旧分支**那条链。

    分叉之后「会话最新状态」与「用户正在看的分支」不再是同一件事：前者是新分支，
    后者是冻结在 cp-2 的根分支。读错的表现是「切回去看到的还是新分支的内容」。
    """
    graph = BranchingGraph(_chain(2))
    runs = _async_service(test_config, thread_store, graph)
    threads = _thread_service(test_config, thread_store, graph)
    await thread_store.create("t1", title="会话")
    await _drain(await runs.regenerate("t1"))

    messages = await threads.history("t1", branch_id="")

    assert len(messages) == 4  # 两轮对话，而不是新分支的一轮


async def test_history_rejects_unknown_branch(test_config, thread_store):
    threads = _thread_service(test_config, thread_store, BranchingGraph(_chain(1)))
    await thread_store.create("t1", title="会话")

    with pytest.raises(NotFoundError):
        await threads.history("t1", branch_id="不存在的分支")
