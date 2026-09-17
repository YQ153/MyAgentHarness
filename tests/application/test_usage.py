"""Token 用量归一化的单元测试。

覆盖：各家字段口径的映射（LangChain 标准 / OpenAI 兼容 / Anthropic /
Ollama）、嵌套容器、非法值、累计器的「累计值」与「多次调用」两种语义。
"""

from __future__ import annotations

import pytest

from application.usage import TokenUsage, UsageAccumulator, normalize_usage


class _FakeMessage:
    """模拟 LangChain 消息：用量挂在属性上而不是字典里。"""

    def __init__(self, usage_metadata=None, response_metadata=None) -> None:
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata


# ------------------------------------------------------------------ 归一化


def test_standard_usage_metadata():
    usage = normalize_usage({"input_tokens": 120, "output_tokens": 30})

    assert usage == TokenUsage(prompt_tokens=120, completion_tokens=30)
    assert usage.total_tokens == 150


def test_openai_compatible_token_usage():
    usage = normalize_usage({"prompt_tokens": 7, "completion_tokens": 11})

    assert usage == TokenUsage(prompt_tokens=7, completion_tokens=11)


def test_anthropic_response_metadata_nested():
    payload = _FakeMessage(
        usage_metadata=None,
        response_metadata={"usage": {"input_tokens": 100, "output_tokens": 5}},
    )

    assert normalize_usage(payload) == TokenUsage(prompt_tokens=100, completion_tokens=5)


def test_openai_response_metadata_nested():
    payload = _FakeMessage(
        usage_metadata={"input_tokens": 3, "output_tokens": 4},
        response_metadata={"token_usage": {"prompt_tokens": 999, "completion_tokens": 999}},
    )

    # WHY 期望标准口径优先：usage_metadata 是 LangChain 的归一化结果，
    # response_metadata 是 provider 原始字段，前者跨 provider 可比
    assert normalize_usage(payload) == TokenUsage(prompt_tokens=3, completion_tokens=4)


def test_ollama_eval_counts():
    usage = normalize_usage({"prompt_eval_count": 64, "eval_count": 128})

    assert usage == TokenUsage(prompt_tokens=64, completion_tokens=128)
    assert usage.total_tokens == 192


def test_ollama_counts_inside_response_metadata():
    payload = _FakeMessage(response_metadata={"prompt_eval_count": 10, "eval_count": 20})

    assert normalize_usage(payload) == TokenUsage(prompt_tokens=10, completion_tokens=20)


def test_missing_usage_returns_none():
    assert normalize_usage({"text": "hello"}) is None
    assert normalize_usage(None) is None
    assert normalize_usage({}) is None


def test_negative_values_are_rejected():
    assert normalize_usage({"input_tokens": -5, "output_tokens": 3}) == TokenUsage(0, 3)


def test_non_numeric_values_are_rejected():
    assert normalize_usage({"input_tokens": "abc", "output_tokens": "2"}) == TokenUsage(0, 2)


def test_all_invalid_returns_none():
    """WHY 覆盖全非法：此时两个计数都拿不到，必须明确返回 None 让调用方
    记 0 并留日志，而不是返回一个全 0 的用量假装「模型没消耗」。"""
    assert normalize_usage({"input_tokens": None, "output_tokens": None}) is None
    assert normalize_usage({"input_tokens": True, "output_tokens": False}) is None


def test_numeric_strings_are_accepted():
    """WHY 接受数字字符串：部分网关把用量以字符串回传，直接丢弃会让
    这些部署永远统计不到用量。"""
    assert normalize_usage({"input_tokens": "12", "output_tokens": "8"}) == TokenUsage(12, 8)


def test_partial_usage_keeps_zero():
    assert normalize_usage({"input_tokens": 9}) == TokenUsage(prompt_tokens=9, completion_tokens=0)


def test_payload_matches_fields():
    assert TokenUsage(2, 3).as_payload() == {
        "prompt_tokens": 2,
        "completion_tokens": 3,
        "total_tokens": 5,
    }


# ------------------------------------------------------------------ 累计器


def test_accumulator_empty_by_default():
    accumulator = UsageAccumulator()

    assert accumulator.empty is True
    assert accumulator.total == TokenUsage(0, 0)


def test_cumulative_chunks_are_not_double_counted():
    """WHY 这条最关键：流式场景下每个分片给的往往是累计值，
    逐片相加会把一次调用放大成几十倍。"""
    accumulator = UsageAccumulator()
    for prompt, completion in ((100, 1), (100, 5), (100, 12), (100, 30)):
        accumulator.add(TokenUsage(prompt, completion))

    assert accumulator.total == TokenUsage(100, 30)


def test_restart_detected_when_counters_drop():
    """一轮里多次模型调用：计数回退意味着新调用开始，前一次的用量要结算。"""
    accumulator = UsageAccumulator()
    accumulator.add(TokenUsage(100, 30))  # 第一次调用
    accumulator.add(TokenUsage(50, 0))  # 第二次调用开始
    accumulator.add(TokenUsage(50, 20))  # 第二次调用结束

    assert accumulator.total == TokenUsage(150, 50)


def test_add_raw_normalizes_payload():
    accumulator = UsageAccumulator()

    assert accumulator.add_raw({"input_tokens": 1, "output_tokens": 2}) is True
    assert accumulator.add_raw({"text": "no usage"}) is False
    assert accumulator.add_raw(None) is False
    assert accumulator.total == TokenUsage(1, 2)
    assert accumulator.empty is False
