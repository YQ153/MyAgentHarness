"""SSE 传输层：把应用事件序列化成 SSE 文本帧。

WHY 放在接口层而不是应用层：SSE 是 HTTP 传输格式，而应用层的 ``AgentEvent``
与传输方式无关——CLI 直接渲染到终端，Web 才走 SSE。把帧格式写进应用层会让
后者被绑死在 HTTP 上，CLI 也不得不为用不到的东西付出概念负担。
"""

from __future__ import annotations

import json

from application.events import AgentEvent

SSE_HEADERS: dict[str, str] = {
    # WHY no-transform 与 X-Accel-Buffering：反向代理默认会缓冲响应，
    # 会让流式输出退化成一次性返回，这两个头是关掉缓冲的标准做法。
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def encode_sse(event: AgentEvent) -> str:
    """把一条应用事件序列化为 SSE 文本帧。

    WHY 手动拼帧而非依赖框架：原生 ``EventSource`` 只支持 GET，
    这里必须走 POST + ReadableStream，因此由服务端保证帧格式正确。

    WHY ``ensure_ascii=False``：中文内容若被转义成 \\uXXXX，虽然可解析，
    但会让 SSE 帧体积翻倍，也妨碍调试时肉眼阅读。

    Args:
        event: 待编码的事件。

    Returns:
        符合 SSE 规范的文本帧，以空行结尾。

    Raises:
        ValueError: ``event`` 为 ``None``。
    """
    if event is None:
        raise ValueError("event 不能为 None")

    body = json.dumps(event.payload, ensure_ascii=False, default=str)
    # WHY 每行都要独立换行：SSE 规范用空行分隔帧，多行数据会产生多帧
    return f"event: {event.event.value}\ndata: {body}\n\n"
