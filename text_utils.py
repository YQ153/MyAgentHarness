"""文本规整工具：空白折叠与截断。

WHY 放在项目根而非 ``application/``：会话标题的规范化在应用层（按展示宽度
截断）与存储层（按入库硬上限截断）都要用到，而分层约定是 application 依赖
runtime。若把它放进 ``application``，``runtime`` 就得反向依赖 ``application``
形成循环导入，因此下沉到与 ``config`` 同级的中立位置。

WHY 需要共享：两层的逻辑原本完全同构，各写一份时改一处漏一处会产出
「前端显示与库中不一致」的问题；而两层的差异其实只是阈值不同——阈值应当作为
参数传入，而不是复制一份代码。

除标题外，两个根级工具插件（``web_tools`` 截断抓取正文、``knowledge_tools`` 截断
检索片段）也在这里取截断逻辑：它们同样不属于任何一层，而「超限才加提示语」这句
判断与标题截断是同一个决策，只放一处才不会出现「有的工具说被截断了、有的不说」。
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
        ValueError: ``text`` 不是字符串，或 ``limit`` 不是 >= 1 的整数。
    """
    if not isinstance(text, str):
        raise ValueError(f"text 必须是字符串，实际：{type(text).__name__}")
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"limit 必须是整数，实际：{type(limit).__name__}")
    if limit < 1:
        raise ValueError(f"limit 必须 >= 1，实际：{limit}")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + _ELLIPSIS


def truncate_with_notice(text: str, limit: int, notice: str) -> str:
    """按字符上限截断，被截断时追加调用方给的提示语；未超限则原样返回。

    WHY 与 :func:`truncate` 并存而不是合并成一个：两者的**长度口径相反**，
    合并只能靠参数切换，反而更难读——

    - :func:`truncate` 面向展示（会话标题）：省略号占一个字符，
      结果总长**不超过** ``limit``；
    - 本函数面向工具输出：保留前 ``limit`` 个字符**之后**再追加提示语，
      结果总长**可以超过** ``limit``。

    真正需要共用的不是口径，而是那句判断：``len(text) <= limit`` 时原样返回、
    否则才追加提示语。各写一份最可能出现的偏差是「提示语无条件追加」——
    于是没被截断的结果也自称被截断了，读者据此丢掉后半段本可用的信息。

    Args:
        text: 待截断文本。
        limit: 保留的字符数上限，必须 >= 1。
        notice: 被截断时追加的提示语，必须是非空字符串。

            WHY 由调用方传入而不是内建：同一份判断服务不同受众——进对话的片段
            用中文、给模型的工具结果用英文并带上具体字符数。措辞随受众演进，
            判断逻辑不该跟着改。

    Returns:
        未超限时原样返回 ``text``；超限时为前 ``limit`` 个字符与 ``notice`` 的拼接。

    Raises:
        ValueError: ``text`` 不是字符串、``limit`` 不是 >= 1 的整数，
            或 ``notice`` 不是非空字符串。
    """
    if not isinstance(text, str):
        raise ValueError(f"text 必须是字符串，实际：{type(text).__name__}")
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"limit 必须是整数，实际：{type(limit).__name__}")
    if limit < 1:
        raise ValueError(f"limit 必须 >= 1，实际：{limit}")
    if not isinstance(notice, str) or not notice:
        raise ValueError(f"notice 必须是非空字符串，实际：{notice!r}")

    if len(text) <= limit:
        return text
    return text[:limit] + notice


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
