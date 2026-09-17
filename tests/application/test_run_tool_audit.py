"""工具调用审计的回归测试。

WHY 单独成文件：工具调用是运行期最频繁、也最容易变成「黑箱」的动作——用户
看到的只是「助手在做某事」。这组用例钉住三件事：审计记录里必须能回答
「哪个工具、发往哪台服务器、耗时多少、成功与否」，内置工具默认不淹没审计，
以及运行被停止时未完成的调用不会被记成成功。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessageChunk, ToolMessage

from agent.mcp import MCPServerStatus
from agent.tooling import ToolBundle
from agent.tools import ToolDescriptor, ToolSource
from application.principal import Principal
from application.run_service import RunService
from application.tool_catalog import ToolCatalog
from tests.application.test_run_governance import RecordingAuditStore
from tests.application.test_run_service import FakeGraphFactory, _drain
from tests.conftest import make_config


def _tool_call_chunk(name: str, args: str, index: int = 0) -> Any:
    """构造携带工具调用分片的模型输出。"""
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"name": name, "args": args, "index": index, "id": f"call-{index}", "type": "tool_call_chunk"}
        ],
    )


def _tool_message(name: str, content: str, status: str = "success") -> ToolMessage:
    """构造工具结果消息。"""
    return ToolMessage(content=content, name=name, tool_call_id=f"call-{name}", status=status)


class ToolCallingGraph:
    """先发起一次工具调用、再回结果、最后结束的图替身。

    节点名从 ``model`` 切到 ``tools`` 正是翻译层判定「工具参数拼接完成」的
    信号，因此这里必须带上 ``langgraph_node`` 元数据，否则 TOOL_CALL 会一直
    留到流末才冲出，顺序与真实运行不一致。

    ``call_name`` 与结果消息的工具名必须一致：审计按名字把「调用」与
    「结果」配对，名字不同会得到一条「有调用无结果」的记录——历史上本替身
    把调用名写死成 ``srv_weather``，导致内置工具用例断言的是另一个工具。
    """

    def __init__(self, *, result: ToolMessage | None = None, call_name: str = "srv_weather") -> None:
        self._result = result
        self._call_name = call_name

    async def astream(
        self,
        payload: Any,
        config: dict[str, Any] | None = None,
        stream_mode: Any = None,
        context: Any = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        yield (
            "messages",
            (_tool_call_chunk(self._call_name, '{"city": "上海"}'), {"langgraph_node": "model"}),
        )
        if self._result is not None:
            yield ("messages", (self._result, {"langgraph_node": "tools"}))


def _catalog(*, with_builtin_flag: bool = False) -> ToolCatalog:
    """构造含一个 MCP 工具的目录（``with_builtin_flag`` 仅用于表达意图）。"""
    return ToolCatalog(
        ToolBundle(
            tools=(),
            descriptors=(
                ToolDescriptor(
                    name="srv_weather",
                    source=ToolSource.MCP,
                    description="查天气",
                    server="srv",
                ),
            ),
            mcp_statuses=(MCPServerStatus(name="srv", transport="stdio", ok=True, tool_count=1),),
        )
    )


def _make_service(
    tmp_path,
    thread_store: Any,
    graph: Any,
    audit: RecordingAuditStore,
    *,
    catalog: ToolCatalog | None = None,
    **config_overrides: Any,
) -> RunService:
    return RunService(
        make_config(tmp_path, **config_overrides),
        thread_store=thread_store,
        graph_factory=FakeGraphFactory(graph),
        audit_store=audit,
        tool_catalog=catalog,
    )


async def test_tool_call_is_audited_with_server_and_duration(tmp_path, thread_store):
    """一条工具调用审计必须能独立回答「谁在什么时候用什么工具做了什么」。"""
    audit = RecordingAuditStore()
    graph = ToolCallingGraph(result=_tool_message("srv_weather", "晴，26℃"))
    service = _make_service(tmp_path, thread_store, graph, audit, catalog=_catalog())

    await _drain(await service.stream("t1", "上海天气"))

    records = audit.of_type("tool_call")
    assert len(records) == 1
    record = records[0]
    assert record["target_id"] == "t1"
    assert record["action"] == "srv_weather"
    assert record["outcome"] == "success"
    assert record["details"]["tool"] == "srv_weather"
    assert record["details"]["server"] == "srv"
    assert record["details"]["source"] == "mcp"
    assert record["details"]["status"] == "success"
    assert record["details"]["elapsed_ms"] >= 0
    assert "上海" in record["details"]["args_preview"]


async def test_tool_error_is_audited_as_error(tmp_path, thread_store):
    audit = RecordingAuditStore()
    graph = ToolCallingGraph(result=_tool_message("srv_weather", "服务不可用", status="error"))
    service = _make_service(tmp_path, thread_store, graph, audit, catalog=_catalog())

    await _drain(await service.stream("t1", "上海天气"))

    record = audit.of_type("tool_call")[0]
    assert record["outcome"] == "error"
    assert record["details"]["status"] == "error"


async def test_tool_call_without_result_is_audited_as_interrupted(tmp_path, thread_store):
    """WHY 必须区分：把「模型请求了工具但没等到结果」记成成功，审计就失去了
    取证价值——运行被停止时最容易出现这种残缺记录。"""
    audit = RecordingAuditStore()
    graph = ToolCallingGraph(result=None)
    service = _make_service(tmp_path, thread_store, graph, audit, catalog=_catalog())

    await _drain(await service.stream("t1", "上海天气"))

    record = audit.of_type("tool_call")[0]
    assert record["outcome"] == "interrupted"
    assert record["details"]["status"] == "interrupted"
    assert record["details"]["elapsed_ms"] is None


async def test_builtin_tool_calls_are_skipped_by_default(tmp_path, thread_store):
    """内置文件/命令工具的调用频次极高，默认不落库以避免淹没审计。"""
    audit = RecordingAuditStore()
    graph = ToolCallingGraph(
        result=_tool_message("read_file", "文件内容"), call_name="read_file"
    )
    service = _make_service(tmp_path, thread_store, graph, audit, catalog=_catalog())

    await _drain(await service.stream("t1", "读一下文件"))

    assert audit.of_type("tool_call") == []


async def test_builtin_tool_calls_recorded_when_enabled(tmp_path, thread_store):
    audit = RecordingAuditStore()
    graph = ToolCallingGraph(
        result=_tool_message("read_file", "文件内容"), call_name="read_file"
    )
    service = _make_service(
        tmp_path,
        thread_store,
        graph,
        audit,
        catalog=_catalog(with_builtin_flag=True),
        tool_audit_builtin=True,
    )

    await _drain(await service.stream("t1", "读一下文件"))

    record = audit.of_type("tool_call")[0]
    assert record["action"] == "read_file"
    assert record["details"]["source"] == "builtin"
    assert record["details"]["server"] is None


async def test_tool_audit_also_records_actor(tmp_path, thread_store):
    """认证开启时审计必须能归因到人，否则「谁调用了这个工具」无解。"""
    audit = RecordingAuditStore()
    graph = ToolCallingGraph(result=_tool_message("srv_weather", "晴"))
    service = _make_service(tmp_path, thread_store, graph, audit, catalog=_catalog())

    await _drain(
        await service.stream("t1", "上海天气", principal=Principal(user_id="alice", role="member"))
    )

    assert audit.of_type("tool_call")[0]["actor_id"] == "alice"


async def test_tool_audit_survives_missing_catalog(tmp_path, thread_store):
    """目录缺失（CLI 精简装配）时仍要留痕，只是来源标为 unknown。"""
    audit = RecordingAuditStore()
    graph = ToolCallingGraph(result=_tool_message("srv_weather", "晴"))
    service = _make_service(tmp_path, thread_store, graph, audit, catalog=None)

    await _drain(await service.stream("t1", "上海天气"))

    record = audit.of_type("tool_call")[0]
    assert record["details"]["source"] == "unknown"
    assert record["details"]["server"] is None
