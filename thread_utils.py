"""会话标识的校验规则：跨层的唯一实现。

WHY 放在项目根而非 ``runtime`` 或 ``application``：这条规则同时被三层使用——
接口层（路径参数校验）、应用层（服务入口校验）、存储层（入库前再兜一次）。
放进 ``application`` 会让 ``runtime`` 反向依赖 ``application``、违反分层契约；
留在 ``runtime`` 则迫使接口层为了合规而绕道一个只做重导出的门面。
下沉到与 ``config`` / ``text_utils`` 同级的中立位置后，三层都只是普通依赖，
规则本身也不必再借道任何一层。

WHY 必须只有一份：此前路由层、服务层与存储层各写了一遍（是否 strip、上限多少、
非字符串如何处理），三处随时可能漂移，而漂移的表现是「接口放行、入库被拒」这类
只在特定输入下才暴露的错误。
"""

from __future__ import annotations

MAX_THREAD_ID_CHARS = 128
"""会话 ID 的字符数硬上限。

WHY 上限必须存在：即便某个调用方漏做校验，也不允许超长标识灌进数据库。
存储层自身也调用 :func:`normalize_thread_id`，这是第二道防线而非唯一一道。
"""


def normalize_thread_id(thread_id: str) -> str:
    """校验会话 ID 并返回规范化结果。

    Args:
        thread_id: 待校验的会话 ID。

    Returns:
        去除首尾空白后的会话 ID。

    Raises:
        ValueError: 非字符串、为空或超出长度上限。
    """
    if not isinstance(thread_id, str):
        raise ValueError(f"thread_id 必须是字符串，实际：{type(thread_id).__name__}")
    normalized = thread_id.strip()
    if not normalized:
        raise ValueError("thread_id 不能为空")
    if len(normalized) > MAX_THREAD_ID_CHARS:
        raise ValueError(f"thread_id 过长（{len(normalized)} > {MAX_THREAD_ID_CHARS}）")
    return normalized
