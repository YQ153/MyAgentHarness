"""Token 用量归一化的单元测试。

覆盖：各家字段口径的映射（LangChain 标准 / OpenAI 兼容 / Anthropic /
Ollama）、嵌套容器、非法值、累计器的「累计值」与「多次调用」两种语义。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from application.usage import (
    TokenUsage,
    UsageAccumulator,
    approximate_tokens,
    calibrated_tokens,
    cache_hit_rate,
    normalize_usage,
    usage_scale_factor,
)


class _FakeMessage:
    """模拟 LangChain 消息：用量挂在属性上而不是字典里。"""

    def __init__(self, usage_metadata=None, response_metadata=None) -> None:
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata


class _Chunk:
    """带响应 id 的模型分片替身（AIMessageChunk 的最小形态）。"""

    def __init__(self, *, id=None, input_tokens=None, output_tokens=None) -> None:
        self.id = id
        self.usage_metadata = (
            {"input_tokens": input_tokens, "output_tokens": output_tokens}
            if input_tokens is not None
            else None
        )


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
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "total_tokens": 5,
    }


# ------------------------------------------------------------------ 缓存命中


def test_deepseek_cache_fields_are_collected():
    """WHY 关键：DeepSeek 的缓存命中价是未命中的 1/50，而命中与否只体现在
    这两个字段上；不采集它们，「这套配置到底有没有省到钱」就无从判断——
    此前「DeepSeek 无缓存收益」的错误判断正是因为没有数据可证伪。"""
    usage = normalize_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_cache_hit_tokens": 900,
            "prompt_cache_miss_tokens": 100,
        }
    )

    assert usage == TokenUsage(
        prompt_tokens=1000,
        completion_tokens=50,
        cache_hit_tokens=900,
        cache_miss_tokens=100,
    )
    assert usage.cache_hit_rate == 0.9
    # 缓存是 prompt 的**细分之一**，不得改变 total 口径
    assert usage.total_tokens == 1050


def test_langchain_input_token_details_are_collected():
    """LangChain 把缓存明细放在 ``input_token_details`` 下，与 input/output 不同层。"""
    usage = normalize_usage(
        {
            "input_tokens": 1000,
            "output_tokens": 50,
            "input_token_details": {"cache_read": 800},
        }
    )

    assert usage == TokenUsage(
        prompt_tokens=1000,
        completion_tokens=50,
        cache_hit_tokens=800,
    )


def test_missing_cache_fields_default_to_zero():
    """provider 未上报缓存时保持 0，且不得因此把整份用量判成 None。

    WHY 仍断言 0.0 而不是 None：``TokenUsage`` 无法区分「未上报」与「确实没命中」
    （两者都是 0），这是数据层面的固有局限；真正的区分靠 provider 是否给了字段。
    绝不能因为缺字段就让整轮用量丢失——那比数字不准更糟。
    """
    usage = normalize_usage({"input_tokens": 10, "output_tokens": 2})

    assert usage == TokenUsage(10, 2)
    assert usage.cache_hit_tokens == 0
    assert usage.cache_hit_rate == 0.0


def test_cached_only_payload_is_not_usage():
    """只有缓存明细、没有主计数的载荷不构成一次用量，必须继续返回 None。"""
    assert normalize_usage({"input_token_details": {"cache_read": 800}}) is None


def test_cache_hit_rate_is_none_without_input():
    assert cache_hit_rate(0, 0) is None
    assert cache_hit_rate(0, 5) is None
    assert cache_hit_rate(-1, 5) is None
    assert TokenUsage(0, 0).cache_hit_rate is None
    assert cache_hit_rate(4, 1) == 0.25


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


def test_accumulator_sums_cache_counters_across_calls():
    """一轮里多次调用的缓存计数必须累加，而不是只留最后一次。"""
    accumulator = UsageAccumulator()
    accumulator.add(TokenUsage(100, 10, cache_hit_tokens=80, cache_miss_tokens=20))
    accumulator.add(TokenUsage(50, 5, cache_hit_tokens=0, cache_miss_tokens=50))

    assert accumulator.total == TokenUsage(
        prompt_tokens=150,
        completion_tokens=15,
        cache_hit_tokens=80,
        cache_miss_tokens=70,
    )
    assert accumulator.total.cache_hit_rate == 80 / 150


def test_accumulator_keeps_cache_counters_within_one_call():
    """同一次调用的累计分片：缓存计数取最新快照，不回退、不叠加。"""
    accumulator = UsageAccumulator()
    accumulator.add(TokenUsage(100, 1, cache_hit_tokens=0, cache_miss_tokens=100))
    accumulator.add(TokenUsage(100, 8, cache_hit_tokens=90, cache_miss_tokens=10))

    assert accumulator.total == TokenUsage(
        prompt_tokens=100,
        completion_tokens=8,
        cache_hit_tokens=90,
        cache_miss_tokens=10,
    )


# ------------------------------------------------------------------ 按调用分段


def test_chunks_with_different_ids_are_separate_calls():
    """WHY 用消息 id 分段：同一响应的所有分片共享 id，id 变化必然是新调用——
    而 prompt 计数在多次调用之间是**递增**的，靠数值回退会把同轮的多次调用
    误并成一次（实测同一轮的两次调用被并成一行，成本被系统性低估）。"""
    accumulator = UsageAccumulator()
    accumulator.add_raw(_Chunk(id="call-1", input_tokens=5000, output_tokens=1))
    accumulator.add_raw(_Chunk(id="call-1", input_tokens=9000, output_tokens=6))
    accumulator.add_raw(_Chunk(id="call-2", input_tokens=12000, output_tokens=14))

    # 同 id 内取最后一片；不同 id 之间相加
    assert accumulator.calls == [TokenUsage(9000, 6), TokenUsage(12000, 14)]
    assert accumulator.total == TokenUsage(21000, 20)


def test_same_id_chunks_take_the_latest_snapshot():
    accumulator = UsageAccumulator()
    accumulator.add_raw(_Chunk(id="c", input_tokens=100, output_tokens=1))
    accumulator.add_raw(_Chunk(id="c", input_tokens=100, output_tokens=9))

    assert accumulator.calls == [TokenUsage(100, 9)]
    assert accumulator.total == TokenUsage(100, 9)


def test_chunks_without_ids_fall_back_to_restart_detection():
    """WHY 保留回退判据：部分 provider / 网关的分片没有 id，此时必须仍能分段，
    不能因为分段信号缺失而丢掉整轮用量。"""
    accumulator = UsageAccumulator()
    accumulator.add(TokenUsage(1000, 1))
    accumulator.add(TokenUsage(2000, 5))  # 递增：不回退，同段
    accumulator.add(TokenUsage(500, 0))  # 回退：新段

    assert accumulator.calls == [TokenUsage(2000, 5), TokenUsage(500, 0)]
    assert accumulator.total == TokenUsage(2500, 5)


def test_mixed_id_presence_falls_back_to_restart_detection():
    """一侧有 id、一侧没有时无法比较，退回计数判据而不是把 id 变化当分段。"""
    accumulator = UsageAccumulator()
    accumulator.add_raw(_Chunk(id=None, input_tokens=1000, output_tokens=1))
    accumulator.add_raw(_Chunk(id="call-1", input_tokens=2000, output_tokens=5))

    assert accumulator.calls == [TokenUsage(2000, 5)]


def test_total_is_zero_when_nothing_was_seen():
    accumulator = UsageAccumulator()

    assert accumulator.calls == []
    assert accumulator.total == TokenUsage(0, 0)
    assert accumulator.empty is True


# ------------------------------------------------------------------ 本地计数


def test_scale_factor_is_at_least_one_and_needs_a_positive_approximation():
    assert usage_scale_factor(200, 100) == 2.0
    # 近似高于真实时不上调：缩放只用于修正「低估」，不把高估再放大
    assert usage_scale_factor(50, 100) == 1.0
    # 没有可信基数时不臆造系数
    assert usage_scale_factor(100, 0) == 1.0
    assert usage_scale_factor(0, 0) == 1.0


def test_scale_factor_rejects_negative_real_tokens():
    with pytest.raises(ValueError, match="real_tokens"):
        usage_scale_factor(-1, 100)


def test_calibrated_tokens_scales_the_approximation():
    messages = [HumanMessage(content="x" * 400)]  # ≈ 100 token（4 字符/token）
    approx = approximate_tokens(messages)
    assert approx >= 100

    assert calibrated_tokens(messages, scale=1.0) == approx
    assert calibrated_tokens(messages, scale=2.0) == approx * 2


def test_calibrated_tokens_rejects_negative_scale():
    with pytest.raises(ValueError, match="scale"):
        calibrated_tokens([HumanMessage(content="hi")], scale=-1)


def test_calibration_restores_cjk_underestimate():
    """中文 1 字 ≈ 0.6–1 token，而近似按 0.25 token/字计——真实约为近似的
    2.5–4 倍。上游的缩放被钳在 1.25，校不动这个量级，所以本项目自建系数。"""
    messages = [HumanMessage(content="汉" * 400)]
    approx = approximate_tokens(messages)
    assert approx < 120  # 400 字 ÷ 4 + 附加，仍未把汉字当独立 token

    ratio = usage_scale_factor(real_tokens=300, approx_tokens=approx)
    assert calibrated_tokens(messages, scale=ratio) >= 300
