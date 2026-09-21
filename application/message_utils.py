"""会话消息的读取工具：角色归一、正文提取与消息链比对。

WHY 独立成模块：这些函数同时服务两处——入口参数解析（``RunService.regenerate`` /
``edit`` 要从历史里找出那条可改写的用户消息）与分叉点定位（``RunBranchService``
要靠整条消息链比对出「用户改口之前」那个检查点）。留在任一侧，另一侧就得反向
依赖对方的内部实现；放在这里，两侧都只依赖一层无状态纯函数。
"""

from __future__ import annotations

from typing import Any


def role_of(message: Any) -> str:
    """把一条图消息的角色归一成 user / assistant / tool / system。

    WHY 不直接用 LangChain 的 ``type``：它以 ``human`` / ``ai`` 命名，而应用层与前端
    一直用 ``user`` / ``assistant``。两套叫法在消息筛选处混用会静默漏判（例如把
    ``human`` 当成未知角色），故在入口处一次性归一。
    """
    kind = getattr(message, "type", "") or ""
    return {"human": "user", "ai": "assistant"}.get(kind, kind or "other")


def message_text(message: Any) -> str:
    """取消息正文；正文是多段内容时拼接其中的文本段。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


def last_user_index(messages: list[Any]) -> int | None:
    """最后一条用户消息的下标；一条都没有时返回 ``None``。"""
    for index in range(len(messages) - 1, -1, -1):
        if role_of(messages[index]) == "user":
            return index
    return None


def user_turn_number(messages: list[Any], index: int) -> int:
    """下标 ``index`` 是第几条用户消息（1 基），用于给分支起一个人能看懂的名字。"""
    return sum(1 for message in messages[: index + 1] if role_of(message) == "user")


def same_message_chain(left: list[Any], right: list[Any]) -> bool:
    """判断两段消息是否同一条历史链。

    WHY 优先比 id 而不是正文：编辑过的消息正文不同，但这里要确认的是「这是同一段
    历史」而不是「文字一样」——正文比较会把两条内容恰好相同的分支判成同一条。
    id 缺失（手写 dict 输入的情形）时退回正文比较。
    """
    if len(left) != len(right):
        return False
    for one, other in zip(left, right):
        left_id = getattr(one, "id", None)
        right_id = getattr(other, "id", None)
        if left_id and right_id:
            if left_id != right_id:
                return False
            continue
        if message_text(one) != message_text(other):
            return False
    return True
