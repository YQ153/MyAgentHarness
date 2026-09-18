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
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from application.errors import (
    NotFoundError,
    OwnershipError,
    PermissionDeniedError,
    ThreadBusyError,
)
from application.run_service import RunService
from application.thread_service import ThreadService
from application.usage_service import UsageService
from tests.application.test_run_service import (
    FakeGraphFactory,
    _drain,
    _make_service,
    _principal,
)
from tests.conftest import make_config


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

    def __init__(self, snapshots: list[Snapshot], chunks: list[Any] | None = None) -> None:
        if not snapshots:
            raise ValueError("至少需要一条快照")
        self._snapshots = snapshots
        self._chunks = chunks or []
        self.astream_configs: list[dict[str, Any]] = []

    def advance_head(self, checkpoint_id: str, messages: list[Any]) -> None:
        """模拟一次运行把会话头推进到新检查点（真实 LangGraph 会写出新记录）。

        WHY 替身需要这个能力：分叉之后会话头与旧分支的头不再相同，而这正是最容易
        被当成恒等式的地方——替身若不推进头，就永远复现不出那类缺陷。
        """
        self._snapshots.insert(0, Snapshot(checkpoint_id, messages))

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
        for chunk in self._chunks:
            yield chunk


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


def _usage_service(config: Any, store: Any, graph: Any, usage_store: Any) -> RunService:
    """带用量存储的 RunService；`_make_service` 不接用量存储，故单独构造。"""
    return RunService(
        config,
        thread_store=store,
        graph_factory=FakeGraphFactory(graph),
        usage_store=usage_store,
    )


def _chunk(text: str = "", **usage: Any) -> tuple[str, Any]:
    """构造带用量字段的消息分片（与 ``test_run_usage`` 同形）。"""
    message = AIMessageChunk(content=text)
    if usage:
        message.usage_metadata = usage
    return ("messages", (message, {"langgraph_node": "model"}))


def _auth_config(tmp_path: Any) -> Any:
    """开启认证的配置；会话密钥是构造前置条件，不补会让用例集体失败。"""
    return make_config(
        tmp_path, auth_mode="apikey", auth_session_secret="测试用会话密钥" * 8
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


async def test_regenerate_records_both_branch_heads(test_config, thread_store):
    """分叉后两条分支各自都记下了自己的头。

    WHY 先跑一轮再分叉：真实会话里的每个检查点都来自一次运行，而「记下自己的头」正是
    在运行收尾时发生的。跳过这一步的替身会造出一个现实中不存在的状态——有检查点、
    却没有任何分支记录，于是断言会红在一个与产品无关的地方。
    """
    graph = BranchingGraph(_chain(2))
    service = _async_service(test_config, thread_store, graph)
    await thread_store.create("t1", title="会话")
    await _drain(await service.stream("t1", "第一个问题"))

    await _drain(await service.regenerate("t1"))

    record = await thread_store.get("t1")
    current = record["current_branch"]
    branches = {row["branch_id"]: row for row in await thread_store.list_branches("t1")}

    assert current != ""
    # 旧分支（根）的位置由它自己上一轮运行收尾时记下——切回来才有地方可回
    assert branches[""]["head_checkpoint"] == "cp-2"
    assert branches[current]["parent_branch_id"] == ""
    assert branches[current]["origin"] == "regenerate"
    # 新分支跑完这一轮后同样记下自己的头
    assert branches[current]["head_checkpoint"] == "cp-2"


async def test_edit_labels_the_branch_with_the_turn_number(test_config, thread_store):
    graph = BranchingGraph(_chain(2))
    service = _async_service(test_config, thread_store, graph)
    await thread_store.create("t1", title="会话")

    await _drain(await service.edit("t1", 2, "改过的第二问"))

    current = (await thread_store.get("t1"))["current_branch"]
    branches = {row["branch_id"]: row for row in await thread_store.list_branches("t1")}
    assert branches[current]["origin"] == "edit"
    assert branches[current]["label"] == "编辑第 2 轮"


async def test_switching_back_reads_the_old_branch_not_the_newest(test_config, thread_store):
    """切回旧分支后必须读到**它自己**的内容。

    复现的是一条真实路径：会话头永远指向最新那条分支，只有当前分支恰好是它时两者
    才相等。切回旧分支后若按会话头去读，旧分支会被显示成新分支的内容——界面上只
    表现为「切了没反应」，没有任何报错。
    """
    graph = BranchingGraph(_chain(2))
    runs = _async_service(test_config, thread_store, graph)
    threads = _thread_service(test_config, thread_store, graph)
    await thread_store.create("t1", title="会话")
    await _drain(await runs.regenerate("t1"))

    # 模拟这次运行写出新检查点：会话头被推进到新分支上，与根分支的头不再相同
    graph.advance_head(
        "cp-run",
        _turn(1) + _turn(2) + [HumanMessage(content="新问", id="nh")],
    )

    await threads.activate_branch("t1", "")

    messages = await threads.history("t1", branch_id="")

    assert len(messages) == 4  # 根分支自己的 4 条，而不是新分支的 5 条


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


# ------------------------------------------------------------------ 用量归属


async def test_regenerate_keeps_the_previous_attempt_usage(
    test_config, thread_store, usage_store
):
    """重生成不得抹掉上一次尝试的成本。

    WHY 断言「总量含两次」而不是「新建一条分支维度的记录」：用量本来就按会话记
    （T10 的口径是「删除会话保留用量」），重生成不删任何既有记录，成本自然不消失。
    若哪天有人图省事在重生成时清一遍旧记录，这条用例会红。

    WHY 用关闭认证的配置：这里要断言的是记账，不是鉴权——开着认证就必须再造一个
    主体，那会让失败指向「谁在调用」而不是「记了多少」。
    """
    graph = BranchingGraph(
        _chain(2), chunks=[_chunk("答", input_tokens=100, output_tokens=10)]
    )
    service = _usage_service(test_config, thread_store, graph, usage_store)
    await thread_store.create("t1", title="会话")

    await _drain(await service.stream("t1", "第一个问题"))
    await _drain(await service.regenerate("t1"))

    summary = await UsageService(
        test_config, usage_store=usage_store, thread_store=thread_store
    ).summarize(thread_id="t1")

    assert summary.run_count == 2
    assert summary.total_tokens == 220  # (100 + 10) × 2 次尝试


# ------------------------------------------------------------------ 权限与归属


async def test_edit_requires_thread_create_permission(tmp_path, thread_store):
    """只读角色（viewer）不得改写他人会话的消息。"""
    service = _make_service(_auth_config(tmp_path), thread_store, BranchingGraph(_chain(2)))
    await thread_store.create("t1", title="会话", owner_id="alice")

    with pytest.raises(PermissionDeniedError):
        await service.edit("t1", 0, "不该成功", principal=_principal("alice", "viewer"))


async def test_edit_requires_ownership(tmp_path, thread_store):
    """有权限但不是会话所有者，同样不得改写。"""
    service = _make_service(_auth_config(tmp_path), thread_store, BranchingGraph(_chain(2)))
    await thread_store.create("t1", title="会话", owner_id="alice")

    with pytest.raises(OwnershipError):
        await service.edit("t1", 0, "不该成功", principal=_principal("bob"))
