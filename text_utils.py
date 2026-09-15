"""文本规整工具：空白折叠与截断。

WHY 放在项目根而非 ``application/``：会话标题的规范化在应用层（按展示宽度
截断）与存储层（按入库硬上限截断）都要用到，而分层约定是 application 依赖
runtime。若把它放进 ``application``，``runtime`` 就得反向依赖 ``application``
形成循环导入，因此下沉到与 ``config`` 同级的中立位置。

WHY 需要共享：两层的逻辑原本完全同构，各写一份时改一处漏一处会产出
「前端显示与库中不一致」的问题；而两层的差异其实只是阈值不同——阈值应当作为
参数传入，而不是复制一份代码。
"""

from __future__ import annotations

from typing import Any

_ELLIPSIS = "…"


def collapse_whitespace(text: Any) -> str:
    """把任意空白（含换行与连续空格）折叠为单个空格并去除首尾。

    WHY 折叠：标题与预览会渲染在单行元素里，保留换行会撑破布局。

    Args:
        text: 任意对象；``None`` 与非字符串会先转成字符串。

    Returns:
        折叠后的文本；输入为空时返回空串。
    """
    if text is None:
        return ""
    return " ".join(str(text).split())


def truncate(text: str, limit: int) -> str:
    """按字符数截断，超出部分以省略号替代。

    WHY 用省略号占一个字符而非直接切片：明确告知用户「内容被截断了」，
    与「内容本来就到此为止」区分开。

    Args:
        text: 待截断文本。
        limit: 保留的字符数上限（含省略号），必须 >= 1。

    Returns:
        截断后的文本。

    Raises:
        ValueError: ``limit`` 不是整数或小于 1。
    """
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"limit 必须是整数，实际：{type(limit).__name__}")
    if limit < 1:
        raise ValueError(f"limit 必须 >= 1，实际：{limit}")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + _ELLIPSIS


def build_title(text: Any, limit: int) -> str:
    """折叠空白后按上限生成单行标题。

    Args:
        text: 原始文本；为空时返回空串。
        limit: 字符上限，含省略号。

    Returns:
        规范化后的标题；输入无有效内容时返回空串。

    Raises:
        ValueError: ``limit`` 非法。
    """
    collapsed = collapse_whitespace(text)
    if not collapsed:
        return ""
    return truncate(collapsed, limit)
