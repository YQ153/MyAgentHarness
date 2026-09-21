"""会话导出文件的人读形态（Markdown）。

WHY 渲染放在应用层而不是接口层：CLI 与 Web 都需要「把一份导出变成人读的文件」，
两边各写一遍必然在细节上分叉（工具调用的呈现、空会话的提示语），而这类差异
用户一眼就能看见。

WHY 只做渲染、不做读取：本模块是纯函数——输入 ``ThreadExport``，输出字符串，
不碰数据库也不碰检查点。导出与导入的语义在 ``ThreadService``，这里只管排版。
"""

from __future__ import annotations

from application.dto import HistoryMessage, ThreadExport

_ROLE_TITLES: dict[str, str] = {
    "human": "用户",
    "ai": "助手",
    "tool": "工具",
    "system": "系统",
}
"""角色到小标题的映射；未列出的角色按原样显示。"""


def _role_title(role: str) -> str:
    """把角色名转成给读者看的标题。"""
    return _ROLE_TITLES.get((role or "").lower(), role or "未知")


def _render_tool_calls(message: HistoryMessage) -> list[str]:
    """把一次助手消息里的工具调用渲染成缩进列表。

    WHY 只列名称与参数、不展开结果：结果在下一条工具消息里，重复呈现会让文件
    在长任务里膨胀一倍，而读者真正关心的是「它调了什么」。
    """
    lines: list[str] = []
    for call in message.tool_calls or []:
        name = str(call.get("name") or "工具")
        args = call.get("args")
        lines.append(f"- `{name}`：`{args if args is not None else ''}`")
    return lines


def render_markdown(payload: ThreadExport) -> str:
    """把导出快照渲染成 Markdown 文本。

    Args:
        payload: 导出快照。

    Returns:
        可直接写入 ``.md`` 文件的文本；消息为空时给出明确提示而不是空文件。
    """
    lines: list[str] = [f"# {payload.title or '未命名会话'}", ""]
    lines.append(f"- 会话 ID：`{payload.thread_id}`")
    if payload.branch_id:
        lines.append(f"- 分支：`{payload.branch_id}`")
    if payload.tags:
        lines.append(f"- 标签：{'、'.join(payload.tags)}")
    if payload.updated_at:
        lines.append(f"- 最近活动：{payload.updated_at}")
    lines.append(f"- 导出时间：{payload.exported_at}")
    lines.append("")

    for note in payload.notes:
        lines.append(f"> {note}")
    if payload.notes:
        lines.append("")

    if not payload.messages:
        lines.append("_这个会话还没有任何消息。_")
        return "\n".join(lines) + "\n"

    for message in payload.messages:
        lines.append(f"## {_role_title(message.role)}")
        lines.append("")
        if message.name:
            lines.append(f"（工具：`{message.name}`）")
            lines.append("")
        # WHY 用围栏包住正文：正文里可能含标题、列表乃至代码块，裸放会让它们
        # 与文件自身的结构混在一起——导出文件被再次阅读时，层级就不再可信。
        if message.content:
            lines.append("```text")
            lines.append(message.content)
            lines.append("```")
        tool_lines = _render_tool_calls(message)
        if tool_lines:
            lines.append("")
            lines.append("调用：")
            lines.extend(tool_lines)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_markdown"]
