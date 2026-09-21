"""RunService 的并发与权限语义回归测试。

WHY 用假图而不是真实 LangGraph 图：这些测试只关心「槽位互斥与释放、
权限与所有权」的应用层语义；真图会引入模型初始化与检查点依赖，
让测试变慢且受 API Key 牵制。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from application.errors import (
    OwnershipError,
    PermissionDeniedError,
    ThreadBusyError,
)
from application.events import AgentEventType
from agent.run_context import ANONYMOUS_USER_ID
from application.principal import Principal
from application.run_service import RunHandle, RunService
from runtime.thread_store import ThreadMetaStore, open_thread_store

from tests.conftest import StubSessionRegistry, make_config


# ------------------------------------------------------------------ 测试替身


class FakeGraph:
    """最小图替身：astream 不产出任何 chunk，一轮运行立即结束。"""

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[Any]:
        # WHY「return + 不可达 yield」：yield 的唯一作用是把本方法标记为
        # 异步生成器，调用方的 `async for` 语法才能成立。
        return
        yield  # noqa: WPS328 不可达，仅为构造异步生成器


class SlowGraph:
    """挂起的图替身：模拟长任务，用于取消语义测试。"""

    def __init__(self, delay: float = 10.0) -> None:
        self._delay = delay

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[Any]:
        await asyncio.sleep(self._delay)
        return
        yield  # noqa: WPS328 不可达，仅为构造异步生成器


class FakeGraphFactory:
    """图工厂替身：模仿 ModelRegistry 的「未知别名抛 KeyError」契约。"""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def get(self, name: str | None = None, *, scope: Any = None) -> Any:
        if name is not None and name != "deepseek-flash":
            raise KeyError(name)
        return self._graph


class MissFirstGetStore(ThreadMetaStore):
    """首次 ``get`` 返回 None 的存储替身。

    WHY：复现「所有权校验时行尚未可见、登记时已被他人认领」的并发竞态，
    验证 ``stream`` 登记后的复查能拦截认领冲突。
    """

    def __init__(self, conn: Any) -> None:
        super().__init__(conn)
        self._missed_once = False

    async def get(self, thread_id: str) -> dict[str, Any] | None:
        if not self._missed_once:
            self._missed_once = True
            return None
        return await super().get(thread_id)


def _make_service(
    config: Any,
    store: ThreadMetaStore,
    graph: Any | None = None,
) -> RunService:
    return RunService(
        config,
        thread_store=store,
        graph_factory=FakeGraphFactory(graph or FakeGraph()),
        workspaces=StubSessionRegistry(config),
    )


def _principal(user_id: str, role: str = "member") -> Principal:
    return Principal(user_id=user_id, role=role)


async def _drain(events: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in events]


# ------------------------------------------------------------------ 构造校验


async def test_constructor_rejects_none_deps(test_config, thread_store):
    """每一项必需依赖为 ``None`` 都要当场失败。

    WHY 把 ``workspaces`` 也列进来：它是「本轮跑在哪个工作区」的唯一来源，缺了它
    就必须要么报错、要么悄悄退回某个默认值——后者正是本次要消除的那类失败。
    """
    workspaces = StubSessionRegistry(test_config)
    graph = FakeGraphFactory(FakeGraph())
    with pytest.raises(ValueError):
        RunService(None, thread_store=thread_store, graph_factory=graph, workspaces=workspaces)
    with pytest.raises(ValueError):
        RunService(test_config, thread_store=None, graph_factory=graph, workspaces=workspaces)
    with pytest.raises(ValueError):
        RunService(test_config, thread_store=thread_store, graph_factory=None, workspaces=workspaces)
    with pytest.raises(ValueError):
        RunService(test_config, thread_store=thread_store, graph_factory=graph, workspaces=None)


# ------------------------------------------------------------------ 输入校验


async def test_stream_rejects_blank_input(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    for blank in ("", "   ", None, 123):
        with pytest.raises(ValueError):
            await service.stream("t1", blank)


async def test_stream_rejects_unknown_model(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    with pytest.raises(KeyError):
        await service.stream("t1", "hello", model_name="gpt-unknown")


# ------------------------------------------------------------------ 运行与槽位


async def test_stream_yields_done_and_releases_slot(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    events = await _drain(await service.stream("t1", "hello world"))

    assert events[-1].event == AgentEventType.DONE
    assert events[-1].payload["thread_id"] == "t1"

    # 槽位已释放：同会话可立即再次发起
    again = await _drain(await service.stream("t1", "next round"))
    assert again[-1].event == AgentEventType.DONE


async def test_busy_slot_rejects_concurrent_run(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    # 拿到生成器但尚未消费：槽位已被占用（占用发生在 stream 返回前）
    pending = await service.stream("t1", "slow one")

    with pytest.raises(ThreadBusyError) as exc_info:
        await service.stream("t1", "second try")
    assert exc_info.value.thread_id == "t1"

    # 互斥是 per-thread 的：其他会话不受影响
    other = await service.stream("t2", "other thread")
    await _drain(other)

    # 消费完第一个生成器后槽位释放
    await _drain(pending)
    resumed = await service.stream("t1", "third try")
    await _drain(resumed)


async def test_cancel_mid_run_releases_slot(test_config, thread_store):
    """WHY 本测试是 stop 功能（T2）的前置契约：取消必须释放槽位，
    否则会话会被永久判定为运行中。"""
    service = _make_service(test_config, thread_store, SlowGraph(delay=10.0))

    generator = await service.stream("t1", "long task")
    task = asyncio.create_task(generator.__anext__())
    # 给生成器一点启动时间，让它真正进入图执行（挂起在 SlowGraph 的 sleep 上）
    await asyncio.sleep(0.05)

    with pytest.raises(ThreadBusyError):
        await service.stream("t1", "during run")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 取消后槽位必须已释放，且会话可重新发起并正常完成
    events = await _drain(await service.stream("t1", "after cancel"))
    assert events[-1].event == AgentEventType.DONE


async def test_stream_records_turn_metadata(test_config, thread_store):
    service = _make_service(test_config, thread_store)

    await _drain(await service.stream("t1", "hello world"))

    record = await thread_store.get("t1")
    assert record is not None
    assert record["turn_count"] == 1
    assert record["title"] == "hello world"
    # disabled 模式下所有者为空串
    assert record["owner_id"] == ""


# ------------------------------------------------------------------ 记忆归属


class ContextRecordingGraph:
    """记录 ``astream`` 收到的 context，用于断言记忆归属确实进了图。"""

    def __init__(self) -> None:
        self.contexts: list[Any] = []

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[Any]:
        self.contexts.append(context)
        return
        yield  # noqa: WPS328 不可达，仅为构造异步生成器


async def test_stream_passes_memory_owner_into_graph_context(tmp_path, thread_store):
    """归属必须随每轮运行进图：命名空间在图内算，缺了它记忆会落进匿名池。"""
    config = make_config(tmp_path, auth_mode="apikey", auth_session_secret="s" * 32)
    graph = ContextRecordingGraph()
    service = _make_service(config, thread_store, graph)

    await _drain(await service.stream("t1", "hello", principal=_principal("alice")))

    assert graph.contexts[0].user_id == "alice"


async def test_resume_passes_memory_owner_into_graph_context(test_config, thread_store):
    """恢复执行同样是「一轮运行」，认证关闭时归属必须与发起时同一口径。"""
    graph = ContextRecordingGraph()
    service = _make_service(test_config, thread_store, graph)
    await _drain(await service.stream("t1", "hello"))

    await _drain(await service.resume("t1", {"decisions": [{"type": "approve"}]}))

    assert graph.contexts[-1].user_id == ANONYMOUS_USER_ID


def test_memory_owner_falls_back_to_anonymous_in_disabled_mode():
    """认证关闭时 owner_id 为空串，必须归一到匿名标识——否则 CLI 与 Web 各写一份。"""
    handle = RunHandle(
        thread_id="t1",
        started_at=0.0,
        cancel_event=asyncio.Event(),
        owner_id="",
    )

    assert handle.memory_owner == ANONYMOUS_USER_ID


def test_memory_owner_prefers_owner_id():
    handle = RunHandle(
        thread_id="t1",
        started_at=0.0,
        cancel_event=asyncio.Event(),
        owner_id="alice",
    )

    assert handle.memory_owner == "alice"


# ------------------------------------------------------------------ 权限与所有权


async def test_apikey_mode_denies_missing_principal(tmp_path, thread_store):
    config = make_config(tmp_path, auth_mode="apikey", auth_session_secret="s" * 32)
    service = _make_service(config, thread_store)

    with pytest.raises(PermissionDeniedError):
        await service.stream("t1", "hello")


async def test_viewer_role_cannot_run(tmp_path, thread_store):
    config = make_config(tmp_path, auth_mode="apikey", auth_session_secret="s" * 32)
    service = _make_service(config, thread_store)

    with pytest.raises(PermissionDeniedError) as exc_info:
        await service.stream("t1", "hello", principal=_principal("v", role="viewer"))
    assert exc_info.value.permission == "thread:create"


async def test_other_user_blocked_from_owned_thread(tmp_path, thread_store):
    config = make_config(tmp_path, auth_mode="apikey", auth_session_secret="s" * 32)
    service = _make_service(config, thread_store)

    alice_events = await _drain(
        await service.stream("t1", "alice was here", principal=_principal("alice"))
    )
    assert alice_events[-1].event == AgentEventType.DONE

    with pytest.raises(OwnershipError):
        await service.stream("t1", "bob tries", principal=_principal("bob"))


async def test_admin_can_run_others_thread(tmp_path, thread_store):
    config = make_config(tmp_path, auth_mode="apikey", auth_session_secret="s" * 32)
    service = _make_service(config, thread_store)

    await _drain(await service.stream("t1", "alice", principal=_principal("alice")))

    events = await _drain(
        await service.stream("t1", "admin takes over", principal=_principal("root", role="admin"))
    )
    assert events[-1].event == AgentEventType.DONE

    # admin 运行不改变归属
    record = await thread_store.get("t1")
    assert record["owner_id"] == "alice"


async def test_resume_requires_hitl_approve_permission(tmp_path, thread_store):
    """WHY 覆盖 T3 的权限拆分：审批让此前被拦下的高危工具真正执行，
    只持有 thread:create 的主体不得恢复运行。"""
    config = make_config(tmp_path, auth_mode="apikey", auth_session_secret="s" * 32)
    service = _make_service(config, thread_store)

    await _drain(await service.stream("t1", "hello", principal=_principal("alice")))

    payload = {"decisions": [{"type": "approve"}]}
    with pytest.raises(PermissionDeniedError) as exc_info:
        await service.resume("t1", payload, principal=_principal("v", role="viewer"))
    assert exc_info.value.permission == "hitl:approve"


async def test_resume_allowed_for_member(tmp_path, thread_store):
    config = make_config(tmp_path, auth_mode="apikey", auth_session_secret="s" * 32)
    service = _make_service(config, thread_store)

    await _drain(await service.stream("t1", "hello", principal=_principal("alice")))

    payload = {"decisions": [{"type": "approve"}]}
    events = await _drain(await service.resume("t1", payload, principal=_principal("alice")))

    assert events[-1].event == AgentEventType.DONE


async def test_resume_rejects_foreign_thread(tmp_path, thread_store):
    config = make_config(tmp_path, auth_mode="apikey", auth_session_secret="s" * 32)
    service = _make_service(config, thread_store)

    await _drain(await service.stream("t1", "hello", principal=_principal("alice")))

    payload = {"decisions": [{"type": "approve"}]}
    with pytest.raises(OwnershipError):
        await service.resume("t1", payload, principal=_principal("bob"))


async def test_concurrent_claim_conflict_detected(tmp_path):
    """WHY 覆盖「校验时行不可见、登记时已被他人认领」的竞态：
    此前 ``_record_turn`` 不返回记录，``stream`` 的登记后复查是死代码，
    Bob 可以静默地在 Alice 的会话上继续运行。"""
    db_path = tmp_path / "race.db"
    async with open_thread_store(db_path) as base:
        # 先让 Alice 直接在存储层认领会话（绕过服务，保证竞态可控）
        await base.record_turn("shared", title_hint="alice", turn_delta=1, owner_id="alice")

        # 借用内层连接构造竞态替身：必须与真实数据共享同一份数据
        race_store = MissFirstGetStore(base._conn)
        config = make_config(tmp_path, auth_mode="apikey", auth_session_secret="s" * 32)
        service = _make_service(config, race_store)

        with pytest.raises(OwnershipError):
            await service.stream("shared", "bob sneaks in", principal=_principal("bob"))
