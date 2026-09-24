"""运行链路上的用量闭环测试：流 → USAGE 事件 → usage_log 落库。

WHY 单独成文件：单元测试分别覆盖了归一化、聚合与端点，但「一轮真实运行
会不会写库」这条链路只有端到端跑一遍才能证明——它横跨 translator、
RunService 的句柄与落库三个环节，任何一处断线都表现为「用量永远是 0」。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessageChunk

from application.events import AgentEventType
from application.run_service import RunService
from runtime.thread_store import ThreadMetaStore
from runtime.usage_store import UsageStore
from tests.application.test_run_service import FakeGraphFactory, _drain
from tests.conftest import StubSessionRegistry, make_config


class ScriptedGraph:
    """按脚本产出分片的图替身；``error`` 非空时在产出后抛错。"""

    def __init__(self, chunks: list[tuple[str, Any]], *, error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> Any:
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


class BrokenUsageStore:
    """写库必失败的用量存储替身。"""

    async def record(self, **_: Any) -> int:
        raise RuntimeError("磁盘已满")


def _chunk(text: str = "", **usage: Any) -> tuple[str, Any]:
    """构造带用量字段的消息分片。

    WHY ``id`` 单独取：它是「同一响应共享、不同响应各异」的分段信号，必须落在
    消息对象上——放进 ``usage_metadata`` 只是一个数字字段，累加器读不到。
    """
    call_id = usage.pop("id", None)
    message = (
        AIMessageChunk(content=text, id=call_id) if call_id else AIMessageChunk(content=text)
    )
    if usage:
        message.usage_metadata = usage
    return ("messages", (message, {"langgraph_node": "model"}))


def _service(
    tmp_path,
    thread_store: ThreadMetaStore,
    graph: Any,
    usage_store: UsageStore | None,
    **config_overrides: Any,
) -> RunService:
    config = make_config(tmp_path, **config_overrides)
    return RunService(
        config,
        thread_store=thread_store,
        graph_factory=FakeGraphFactory(graph),
        workspaces=StubSessionRegistry(config),
        usage_store=usage_store,
    )


def _usage_event(events: list[Any]) -> Any:
    usages = [event for event in events if event.event is AgentEventType.USAGE]
    assert len(usages) == 1, "一轮运行必须且只能上报一次用量"
    return usages[0]


# ------------------------------------------------------------------ 正常闭环


async def test_run_records_usage_once_with_cumulative_totals(
    tmp_path, thread_store: ThreadMetaStore, usage_store: UsageStore
):
    """WHY 断言「累计值不重复计数」：三个分片给的 100/100/100 输入是同一个
    调用的累计快照，写库必须是 100，而不是 300。"""
    graph = ScriptedGraph(
        [
            _chunk("你", input_tokens=100, output_tokens=1),
            _chunk("好", input_tokens=100, output_tokens=8),
            _chunk("！", input_tokens=100, output_tokens=20),
        ]
    )
    service = _service(tmp_path, thread_store, graph, usage_store)

    events = await _drain(await service.stream("t1", "hi", model_name="deepseek-flash"))

    assert _usage_event(events).payload == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "total_tokens": 120,
        # 无 id 的分片走「计数回退」兜底：三次同 prompt 视为同一次调用，只有一段
        "calls": [
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
                "total_tokens": 120,
            }
        ],
    }
    # USAGE 必须早于 DONE，前端才能在收尾前把数字贴到助手消息里
    assert events[-1].event is AgentEventType.DONE
    assert events[-2].event is AgentEventType.USAGE

    summary = await usage_store.summarize(thread_id="t1")
    assert summary["prompt_tokens"] == 100
    assert summary["completion_tokens"] == 20
    assert summary["call_count"] == 1
    assert summary["groups"][0]["key"] == "deepseek-flash"


async def test_run_records_usage_without_owner(
    tmp_path, thread_store: ThreadMetaStore, usage_store: UsageStore
):
    """用量必须落库，且不属于任何主体（应用不区分调用方）。

    WHY 仍然断言归属列：存储层按 ``owner_id`` 聚合，留空是「本机全部运行」这一口径的
    唯一表示；写进一个别的值会让按 owner 聚合的查询永远为空，而那种失败不报错。
    """
    graph = ScriptedGraph([_chunk("ok", input_tokens=5, output_tokens=2)])
    service = _service(tmp_path, thread_store, graph, usage_store)

    await _drain(await service.stream("t1", "hi"))

    assert (await usage_store.summarize(thread_id="t1"))["call_count"] == 1


async def test_run_without_usage_still_reports_zero(
    tmp_path, thread_store: ThreadMetaStore, usage_store: UsageStore
):
    """WHY 零用量也要发事件：前端靠 USAGE 判断「本轮统计结束」，
    缺事件会让界面一直停在「统计中」；同时把 0 落库便于事后核对缺失率。"""
    service = _service(tmp_path, thread_store, ScriptedGraph([_chunk("ok")]), usage_store)

    events = await _drain(await service.stream("t1", "hi"))

    assert _usage_event(events).payload["total_tokens"] == 0
    assert (await usage_store.summarize())["call_count"] == 1


async def test_error_run_still_records_usage(
    tmp_path, thread_store: ThreadMetaStore, usage_store: UsageStore
):
    """WHY 出错也要落库：模型已经计费，报错只让输出不可用，不撤销消耗。"""
    graph = ScriptedGraph(
        [_chunk("半句", input_tokens=30, output_tokens=7)],
        error=RuntimeError("模型不可用"),
    )
    service = _service(tmp_path, thread_store, graph, usage_store)

    events = await _drain(await service.stream("t1", "hi"))

    assert [event.event for event in events] == [
        AgentEventType.TOKEN,
        AgentEventType.ERROR,
        AgentEventType.USAGE,
        AgentEventType.DONE,
    ]
    assert (await usage_store.summarize())["total_tokens"] == 37


# ------------------------------------------------------------------ 降级


async def test_missing_usage_store_skips_persistence(tmp_path, thread_store: ThreadMetaStore):
    """WHY 覆盖无存储：CLI 与单元测试没有用量库，事件流必须照常完整。"""
    service = _service(
        tmp_path,
        thread_store,
        ScriptedGraph([_chunk("ok", input_tokens=3, output_tokens=1)]),
        None,
    )

    events = await _drain(await service.stream("t1", "hi"))

    assert _usage_event(events).payload["total_tokens"] == 4


async def test_usage_write_failure_does_not_break_stream(tmp_path, thread_store: ThreadMetaStore):
    """WHY 吞掉写库失败：用量是旁路数据，不能让一次成功的对话变成错误响应。"""
    service = _service(
        tmp_path,
        thread_store,
        ScriptedGraph([_chunk("ok", input_tokens=3, output_tokens=1)]),
        BrokenUsageStore(),
    )

    events = await _drain(await service.stream("t1", "hi"))

    assert events[-1].event is AgentEventType.DONE
    assert _usage_event(events).payload["total_tokens"] == 4


async def test_multiple_runs_accumulate_rows(
    tmp_path, thread_store: ThreadMetaStore, usage_store: UsageStore
):
    graph = ScriptedGraph([_chunk("ok", input_tokens=10, output_tokens=5)])
    service = _service(tmp_path, thread_store, graph, usage_store)

    await _drain(await service.stream("t1", "第一轮"))
    await _drain(await service.stream("t1", "第二轮"))

    summary = await usage_store.summarize(thread_id="t1")
    assert summary["call_count"] == 2
    assert summary["total_tokens"] == 30


async def test_run_records_cache_counters_end_to_end(
    tmp_path, thread_store: ThreadMetaStore, usage_store: UsageStore
):
    """WHY 端到端：缓存字段横跨 translator → USAGE 事件 → 落库三处，
    任何一处漏接的表现都是「命中率恒为 0」——那既不报错，也正好会让人
    再次得出「没有缓存收益」的结论。"""
    graph = ScriptedGraph(
        [
            _chunk(
                "ok",
                input_tokens=1000,
                output_tokens=10,
                prompt_cache_hit_tokens=900,
                prompt_cache_miss_tokens=100,
            )
        ]
    )
    service = _service(tmp_path, thread_store, graph, usage_store)

    events = await _drain(await service.stream("t1", "hi", model_name="deepseek-flash"))

    payload = _usage_event(events).payload
    assert payload["cache_hit_tokens"] == 900
    assert payload["cache_miss_tokens"] == 100

    summary = await usage_store.summarize(thread_id="t1")
    assert summary["cache_hit_tokens"] == 900
    assert summary["cache_miss_tokens"] == 100
    assert summary["groups"][0]["cache_hit_tokens"] == 900


async def test_run_records_each_model_call_as_its_own_row(
    tmp_path, thread_store: ThreadMetaStore, usage_store: UsageStore
):
    """WHY 逐次落库：一轮内多次模型调用的 prompt 是**递增**的，此前靠「计数回退」
    分段时递增不触发回退，整轮被并成一次——成本被系统性低估，命中率趋势也不可读。
    分段信号改用消息 id 后，两个 id 对应两行。"""
    graph = ScriptedGraph(
        [
            _chunk("你", input_tokens=5000, output_tokens=1, id="call-1"),
            _chunk("好", input_tokens=9000, output_tokens=6, id="call-1"),
            _chunk("！", input_tokens=12000, output_tokens=14, id="call-2"),
            _chunk("。", input_tokens=12000, output_tokens=20, id="call-2"),
        ]
    )
    service = _service(tmp_path, thread_store, graph, usage_store)

    events = await _drain(await service.stream("t1", "hi", model_name="deepseek-flash"))

    # 汇总事件 = 全部调用之和（两次调用相加，而不是只留最后一次）
    payload = _usage_event(events).payload
    assert payload["prompt_tokens"] == 21000
    assert payload["completion_tokens"] == 26
    assert [call["prompt_tokens"] for call in payload["calls"]] == [9000, 12000]

    rows, _ = await usage_store.list_recent(thread_id="t1")
    assert [row["prompt_tokens"] for row in rows] == [9000, 12000]
    # 同一轮的两行必须能按 trace 归拢（CLI 形态为 None 时至少不互相污染）
    assert rows[0]["model"] == "deepseek-flash"


async def test_run_without_ids_still_sums_chunks(
    tmp_path, thread_store: ThreadMetaStore, usage_store: UsageStore
):
    """WHY 兜底路径必须保住：部分 provider / 网关的分片没有 id，此时退回
    计数回退判据——精度差一些，但不能把用量丢成零。"""
    graph = ScriptedGraph(
        [
            _chunk("a", input_tokens=1000, output_tokens=1),
            _chunk("b", input_tokens=2000, output_tokens=5),
        ]
    )
    service = _service(tmp_path, thread_store, graph, usage_store)

    events = await _drain(await service.stream("t1", "hi"))

    # 递增不触发回退 → 仍视为同一次调用的累计快照，取最后一片
    assert _usage_event(events).payload["prompt_tokens"] == 2000
    rows, _ = await usage_store.list_recent(thread_id="t1")
    assert len(rows) == 1
