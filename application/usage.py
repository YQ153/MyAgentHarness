"""Token 用量的提取与归一化。

职责边界：只回答「这一轮用了多少 token」，不关心它是谁的、要不要落库。

WHY 独立成模块：各家 provider 的用量字段命名互不兼容（LangChain 标准口径是
``input_tokens`` / ``output_tokens``，OpenAI 兼容端常见 ``prompt_tokens`` /
``completion_tokens``，Ollama 是 ``prompt_eval_count`` / ``eval_count``，且可能
藏在 ``usage_metadata`` 或 ``response_metadata.usage`` 里）。把这套映射塞进
事件翻译器会让「翻译事件」与「换算用量」两件事互相纠缠，也无法单独测试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_INPUT_KEYS: tuple[str, ...] = (
    "input_tokens",
    "prompt_tokens",
    "input_token_count",
    "prompt_eval_count",
)
"""各家 provider 表示「输入（prompt）token 数」的字段名。"""

_OUTPUT_KEYS: tuple[str, ...] = (
    "output_tokens",
    "completion_tokens",
    "output_token_count",
    "eval_count",
)
"""各家 provider 表示「输出（completion）token 数」的字段名。"""

_USAGE_CONTAINERS: tuple[str, ...] = ("usage_metadata", "usage", "token_usage")
"""可能承载用量的一层容器字段。"""

_RESPONSE_METADATA_KEY = "response_metadata"
"""LangChain 消息上保存原始响应信息的字段。"""


@dataclass(frozen=True)
class TokenUsage:
    """一次（或累计若干次）模型调用的 token 用量。

    WHY 只留 prompt / completion 两个计数：``total_tokens`` 在部分 provider
    上还包含缓存读取、推理 token 等额外项，各家的 ``total`` 口径不一致；
    统一由二者相加得到，跨 provider 才可比。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """输入与输出之和。"""
        return self.prompt_tokens + self.completion_tokens

    def as_payload(self) -> dict[str, int]:
        """转成事件载荷（前端与落库共用同一套字段名）。"""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def _coerce_int(value: Any) -> int | None:
    """把字段值转成非负整数；无法转换时返回 ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        number = int(value.strip())
        return number if number >= 0 else None
    return None


def _read_counter(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    """按候选字段名依次读取，返回首个有效值。"""
    for key in keys:
        if key not in mapping:
            continue
        number = _coerce_int(mapping[key])
        if number is not None:
            return number
    return None


def _usage_from_mapping(mapping: Mapping[str, Any]) -> TokenUsage | None:
    """从一份映射里解析用量；两个计数都没找到时返回 ``None``。"""
    prompt = _read_counter(mapping, _INPUT_KEYS)
    completion = _read_counter(mapping, _OUTPUT_KEYS)
    if prompt is None and completion is None:
        return None
    return TokenUsage(prompt_tokens=prompt or 0, completion_tokens=completion or 0)


def _candidate_mappings(payload: Any) -> list[Mapping[str, Any]]:
    """列出待解析的映射，按优先级从高到低。

    WHY 需要展开容器：同一份用量可能出现在消息对象的 ``usage_metadata``
    （LangChain 标准口径）、``response_metadata.usage``（Anthropic）、
    ``response_metadata.token_usage``（OpenAI 兼容端）或直接躺在
    ``response_metadata`` 上（Ollama）。逐个尝试即可，无需按 provider 分支。
    """
    candidates: list[Mapping[str, Any]] = []

    def collect(node: Any, depth: int) -> None:
        if node is None or depth > 2:
            return
        if isinstance(node, Mapping):
            candidates.append(node)
            for key in _USAGE_CONTAINERS:
                collect(node.get(key), depth + 1)
            collect(node.get(_RESPONSE_METADATA_KEY), depth + 1)
            return
        # 对象形态（LangChain 消息）：只认约定的属性，不做任意反射遍历
        for attribute in (*_USAGE_CONTAINERS, _RESPONSE_METADATA_KEY):
            collect(getattr(node, attribute, None), depth + 1)

    collect(payload, 0)
    return candidates


def normalize_usage(payload: Any) -> TokenUsage | None:
    """把任意 provider 的用量字段归一化成 :class:`TokenUsage`。

    Args:
        payload: 用量字典、含用量字段的响应字典，或 LangChain 消息对象。

    Returns:
        归一化后的用量；``None`` 表示这份载荷里没有可识别的用量字段
        （调用方应记 0 并留日志，而不是静默丢弃这一轮）。
    """
    if payload is None:
        return None

    for mapping in _candidate_mappings(payload):
        usage = _usage_from_mapping(mapping)
        if usage is not None:
            return usage
    return None


class UsageAccumulator:
    """累计一轮运行中所有模型调用的用量。

    WHY 不能简单地逐片相加：流式响应里每个 chunk 携带的计数在多数 provider
    上是**累计值**（OpenAI 兼容端在最后一个 chunk 给出全量，Anthropic 在
    ``message_delta`` 里给累计输出），直接相加会把一次调用放大成几十倍。

    WHY 也不能只取最后一个值：一轮运行通常包含多次模型调用（思考 → 调工具 →
    再思考），每次调用的计数都从自己的小数值重新开始，只留最后一个会漏掉
    之前所有调用。

    采用的判据：计数**回退**即意味着新的一次调用开始了（累计计数在一次调用
    内单调不减），此时把上一次的用量结算进已提交部分。这是在不侵入 provider
    适配器的前提下能拿到的最好精度。
    """

    def __init__(self) -> None:
        self._committed = TokenUsage(0, 0)
        self._current: TokenUsage | None = None

    def add(self, usage: TokenUsage | None) -> bool:
        """并入一份用量快照。

        Args:
            usage: 归一化后的用量；``None`` 表示本次没有用量，直接忽略。

        Returns:
            是否确实并入了数据（``False`` 表示该分片没有用量字段）。
        """
        if usage is None:
            return False

        if self._current is None:
            self._current = usage
            return True

        is_new_call = (
            usage.prompt_tokens < self._current.prompt_tokens
            or usage.total_tokens < self._current.total_tokens
        )
        if is_new_call:
            self._committed = TokenUsage(
                prompt_tokens=self._committed.prompt_tokens + self._current.prompt_tokens,
                completion_tokens=self._committed.completion_tokens
                + self._current.completion_tokens,
            )
            self._current = usage
            return True

        # 同一次调用内的后续分片：取较新的（更大的）累计值
        self._current = usage
        return True

    def add_raw(self, payload: Any) -> bool:
        """归一化并入一份原始载荷（消息对象或响应字典）。"""
        return self.add(normalize_usage(payload))

    @property
    def total(self) -> TokenUsage:
        """本轮累计用量；从未取到用量时为零值。"""
        if self._current is None:
            return self._committed
        return TokenUsage(
            prompt_tokens=self._committed.prompt_tokens + self._current.prompt_tokens,
            completion_tokens=self._committed.completion_tokens
            + self._current.completion_tokens,
        )

    @property
    def empty(self) -> bool:
        """是否从未取到任何用量。"""
        return self._current is None and self._committed.total_tokens == 0


__all__ = ["TokenUsage", "UsageAccumulator", "normalize_usage"]
