"""事件翻译器的「截断留存」接线测试。

WHY 单独覆盖这条接线：预览截断与留存引用分处两层（翻译器算引用、RunService 落盘），
接错的表现是「界面上有链接、点开是 404」——一个只在真实使用中才会被发现的静默缺口。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage

from application.event_translator import LangGraphEventTranslator

_LONG = "x" * 5000


def _tool_result(translator: LangGraphEventTranslator, *, content: Any = _LONG, name: str = "execute"):
    """直接驱动工具结果分支，返回产出的事件列表。"""
    return translator._on_tool_message(  # noqa: SLF001 私有分支是本组被测对象
        ToolMessage(content=content, name=name, tool_call_id="call-1")
    )


def test_truncated_result_carries_retention_reference():
    captured: list[tuple[str, str]] = []

    def capture(tool_name: str, full_text: str) -> str:
        captured.append((tool_name, full_text))
        return "/_tool_outputs/t1/0001-execute.txt"

    translator = LangGraphEventTranslator(
        tool_result_preview_limit=10, full_output_capture=capture
    )

    payload = _tool_result(translator)[0].payload

    assert payload["truncated"] is True
    assert payload["preview"] == _LONG[:10]
    # 事件里只能有引用、不能有正文：事件会被序列化成 SSE 发给浏览器
    assert payload["full_output_ref"] == "/_tool_outputs/t1/0001-execute.txt"
    assert len(payload["preview"]) == 10
    assert captured == [("execute", _LONG)]


def test_short_result_is_not_captured():
    calls: list[str] = []

    translator = LangGraphEventTranslator(
        tool_result_preview_limit=1000,
        full_output_capture=lambda name, text: calls.append(name) or "/never.txt",
    )

    payload = _tool_result(translator, content="short")[0].payload

    assert payload["truncated"] is False
    assert payload["full_output_ref"] is None
    # 没有截断就没有留存：白写一份与预览等长的副本只是浪费磁盘
    assert calls == []


def test_capture_failure_degrades_to_no_reference():
    """留存是旁路能力：它失败不该让一轮已经跑完的对话崩掉，但也不能带着坏引用。"""

    def broken(name: str, text: str) -> str:
        raise OSError("磁盘满了")

    translator = LangGraphEventTranslator(
        tool_result_preview_limit=10, full_output_capture=broken
    )

    payload = _tool_result(translator)[0].payload

    assert payload["truncated"] is True
    assert payload["full_output_ref"] is None


def test_without_capture_there_is_no_reference():
    translator = LangGraphEventTranslator(tool_result_preview_limit=10)

    payload = _tool_result(translator)[0].payload

    assert payload["truncated"] is True
    assert payload["full_output_ref"] is None


def test_non_string_content_is_always_treated_as_truncated():
    """内容块（多模态）无法确定文本化后的完整长度，必须保守标记为已截断。"""
    translator = LangGraphEventTranslator(tool_result_preview_limit=10)

    payload = _tool_result(
        translator, content=[{"type": "text", "text": "x" * 100}]
    )[0].payload

    assert payload["truncated"] is True
