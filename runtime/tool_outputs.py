"""工具输出的完整内容留存。

WHY 需要落盘：``application.event_translator`` 只把工具结果的前若干字符放进事件，
超出部分当场丢弃——界面上只剩一句「(输出已截断)」。留存把这件事从**信息丢失**
变成**可回取**：用户能看完整结果，也能把它交给别的工具或人。

WHY 不把完整正文放进事件：事件会被序列化成 SSE 发给浏览器，把完整正文塞进去等于
把预览上限彻底废掉（一次工具调用可能是几十万字符）。因此事件里只带**引用**，
正文落盘，由文件面板按需取。

WHY 目录名以 ``_`` 开头：让它与 Agent 自己的工作产物在文件面板里一眼可辨——
``_tool_outputs`` 是系统的旁路留存，不是任务成果。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

OUTPUT_DIR_NAME = "_tool_outputs"
"""工具输出留存目录名（位于工作区根下）。"""

_SEQUENCE_WIDTH = 4
"""序号宽度：定宽零填充让文件名的字典序等于时间序，清理旧文件时才不必解析时间。"""

_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_NAME_CHARS = 40


def sanitize_tool_name(name: str) -> str:
    """把工具名压成可安全用作文件名的一段。

    WHY 必须清洗：工具名来自工具注册表（含 MCP 服务器提供的名字），可能带 ``/``、
    空格或非 ASCII 字符——直接拼进路径轻则建出多层目录，重则在 Windows 上撞上
    非法字符建不出文件。

    Args:
        name: 原始工具名。

    Returns:
        仅含 ``[A-Za-z0-9_.-]`` 的片段；清洗后为空时返回 ``tool``。
    """
    cleaned = _UNSAFE_NAME_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.strip("._")[:_MAX_NAME_CHARS]
    return cleaned or "tool"


def tool_output_path(root: Path, thread_id: str, sequence: int, tool_name: str) -> Path:
    """算出某次工具输出的留存路径。

    Args:
        root: 工作区根目录。
        thread_id: 会话标识（按会话分目录，便于整体清理）。
        sequence: 本轮内的第几次留存（从 1 开始）。
        tool_name: 工具名。

    Returns:
        ``<root>/_tool_outputs/<thread_id>/0001-<tool>.txt``。
    """
    safe_thread = sanitize_tool_name(thread_id) or "thread"
    filename = f"{sequence:0{_SEQUENCE_WIDTH}d}-{sanitize_tool_name(tool_name)}.txt"
    return Path(root) / OUTPUT_DIR_NAME / safe_thread / filename


def write_tool_output(path: Path, text: str, *, max_chars: int) -> None:
    """把完整输出写入留存文件。

    WHY 仍然设字符上限：留存是为了「能回取」，不是为了做无限仓库。一次读到几十 MB
    文件的工具调用若原样落盘，磁盘会随对话量无界增长，而截断后的副本配上首行说明
    已经足以回答「这次调用产出了什么」。

    Args:
        path: 目标文件路径。
        text: 完整输出文本。
        max_chars: 单个留存文件的字符上限。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if len(text) > max_chars:
        text = (
            f"{text[:max_chars]}\n\n"
            f"... Output truncated at {max_chars} chars (retained copy of a larger result)."
        )
    # WHY newline=""：文本模式在 Windows 上会把 \n 改写成 \r\n，于是「留存完整输出」
    # 实际留存的是一份**被改写过的**副本（实测一次 22,441 字符的输出多出 801 个字节），
    # 而回取时又会被读回成另一个形状。留存的价值在于逐字节原样，不是「差不多一样」。
    path.write_text(text, encoding="utf-8", newline="")


def prune_tool_outputs(thread_dir: Path, *, keep: int) -> int:
    """按上限清理同一会话下的旧留存，返回删除数量。

    WHY 必须有上限：留存目录只增不减的话，长期运行的部署会把它堆成磁盘黑洞，
    而真正有用的其实只有最近若干次——旧输出对应的是已经翻过去的对话。

    Args:
        thread_dir: 某会话的留存目录。
        keep: 保留的最新文件数。

    Returns:
        实际删除的文件数。
    """
    if keep < 1 or not thread_dir.is_dir():
        return 0

    files = sorted(
        (item for item in thread_dir.iterdir() if item.is_file()),
        # 文件名定宽零填充 → 字典序即时间序，无需解析时间
        key=lambda item: item.name,
    )
    stale = files[: max(0, len(files) - keep)]
    removed = 0
    for item in stale:
        try:
            item.unlink()
            removed += 1
        except OSError:
            # 单个文件删不掉（被占用等）不该让整轮对话失败，也不该中断其余清理
            logger.warning("工具输出留存清理失败：%s", item)
    return removed


__all__ = [
    "OUTPUT_DIR_NAME",
    "prune_tool_outputs",
    "sanitize_tool_name",
    "tool_output_path",
    "write_tool_output",
]
