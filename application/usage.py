"""Token 用量的提取与归一化。

职责边界：只回答「这一轮用了多少 token」，不关心它是谁的、要不要落库。

WHY 独立成模块：各家 provider 的用量字段命名互不兼容（LangChain 标准口径是
``input_tokens`` / ``output_tokens``，OpenAI 兼容端常见 ``prompt_tokens`` /
``completion_tokens``，Ollama 是 ``prompt_eval_count`` / ``eval_count``，且可能
藏在 ``usage_metadata`` 或 ``response_metadata.usage`` 里）。把这套映射塞进
事件翻译器会让「翻译事件」与「换算用量」两件事互相纠缠，也无法单独测试。

WHY 还要采集缓存命中：DeepSeek 的上下文硬盘缓存默认开启，命中价为未命中的
1/50（``deepseek-flash`` 空闲时段 0.02 元 vs 1 元每百万 token），而命中与否
只体现在 ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens`` 两个字段上。
不采集这两个字段，缓存收益在用量面板上完全不可见——此前「DeepSeek 无缓存收益」
这一判断正是因为没有数据可证伪才长期残留（见 ``agent/graph.py`` 的中间件注释）。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any, Mapping

from langchain_core.messages.utils import count_tokens_approximately

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

_CACHE_HIT_KEYS: tuple[str, ...] = (
    "prompt_cache_hit_tokens",
    "cache_read_input_tokens",
    "cache_read",
)
"""表示「输入中命中缓存」的 token 数。

WHY 收三种口径：DeepSeek 用 ``prompt_cache_hit_tokens``（OpenAI 兼容格式，
直接躺在 ``usage`` 里）；Anthropic 用 ``cache_read_input_tokens``；LangChain
归一化后放在 ``usage_metadata.input_token_details.cache_read``。三者语义一致
（都表示「这次没为这些 token 重新计算」），因此可以合并识别。
"""

_CACHE_MISS_KEYS: tuple[str, ...] = ("prompt_cache_miss_tokens",)
"""表示「输入中未命中缓存」的 token 数。

WHY 只认 DeepSeek 的口径：Anthropic 的 ``cache_creation_input_tokens`` 是
「写入缓存」而非「未命中」，把它当 miss 会高估未命中规模；宁缺毋滥。

WHY 该字段在 DeepSeek 上恒为 0（2026-09-23 查上游源码确认）：DeepSeek 的响应里
**有** ``prompt_cache_miss_tokens``，但 ``langchain_deepseek`` 只把
``prompt_cache_hit_tokens`` 透传成 ``usage_metadata.input_token_details.cache_read``
（见其 ``_add_cache_read_tokens``，注释写明「Only cache hits are recorded」），
**从不读取 miss 字段**。所以这里的 0 含义是「上游没透传」，而不是「未命中为 0」——
这两者在数值上无法区分，只能靠读上游源码才能判定。

WHY 未命中量照样能算：上游 docstring 写明 ``prompt_tokens`` 等于
``prompt_cache_hit_tokens + prompt_cache_miss_tokens``，因此
``prompt_tokens - cache_hit_tokens`` 就是真实的未命中量。
"""

_USAGE_CONTAINERS: tuple[str, ...] = (
    "usage_metadata",
    "usage",
    "token_usage",
    "input_token_details",
)
"""可能承载用量的一层容器字段。

WHY 含 ``input_token_details``：LangChain 把缓存明细单独放这一层
（``usage_metadata.input_token_details.cache_read``），而 input/output 留在
``usage_metadata`` 顶层——缓存字段因此跨两层，见 ``normalize_usage`` 的补齐逻辑。
"""

_RESPONSE_METADATA_KEY = "response_metadata"
"""LangChain 消息上保存原始响应信息的字段。"""


def cache_hit_rate(prompt_tokens: int, cache_hit_tokens: int) -> float | None:
    """计算缓存命中率（0..1 之间的小数）。

    WHY 返回 ``None`` 而不是 0：输入为 0 时（例如全非法字段被丢弃、或首次
    调用还没进上下文）「命中率」在数学上无定义，报 0 会被读成「缓存全没命中」，
    而那是另一个结论。

    Args:
        prompt_tokens: 输入 token 总数。
        cache_hit_tokens: 输入中命中缓存的 token 数。

    Returns:
        命中率；输入非正时返回 ``None``。
    """
    if prompt_tokens <= 0:
        return None
    return cache_hit_tokens / prompt_tokens


@dataclass(frozen=True)
class TokenUsage:
    """一次（或累计若干次）模型调用的 token 用量。

    WHY 只留 prompt / completion 两个计数：``total_tokens`` 在部分 provider
    上还包含缓存读取、推理 token 等额外项，各家的 ``total`` 口径不一致；
    统一由二者相加得到，跨 provider 才可比。

    WHY 缓存计数不改变 ``total_tokens`` 的口径：它们描述的是 ``prompt_tokens``
    内部的构成，而不是额外消耗，因此 total 仍由 prompt + completion 得到。

    实测口径（2026-09-23，``deepseek-flash``，会话 df0b66e5…）：``cache_miss_tokens``
    恒为 0，而 ``prompt_tokens`` 大于 ``cache_hit_tokens``（如 7571 − 7040 = 531）。
    成因是上游 SDK 不透传 miss 字段，**而不是**「命中 + 未命中 ≠ 输入」——上游口径
    本身就是 ``prompt = hit + miss``（详见 ``_CACHE_MISS_KEYS``）。因此命中率的分母
    取 ``prompt_tokens``，未命中量按 ``prompt_tokens - cache_hit_tokens`` 估算。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """输入与输出之和。"""
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        """输入侧的缓存命中率；无法判定时为 ``None``。"""
        return cache_hit_rate(self.prompt_tokens, self.cache_hit_tokens)

    def as_payload(self) -> dict[str, int]:
        """转成事件载荷（前端与落库共用同一套字段名）。

        WHY 不含命中率：命中率是由两个计数派生的比例，放进载荷就等于把
        「谁按什么公式算」复制到每个消费方；载荷只带原始计数，比例由
        ``cache_hit_rate`` 统一给出。
        """
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
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


def _read_counter_across(
    mappings: list[Mapping[str, Any]], keys: tuple[str, ...]
) -> int | None:
    """在**多层**映射里依次查找同一个计数，返回首个有效值。

    WHY 需要跨层：缓存命中数在部分口径下与 input/output 不在同一层
    （LangChain 把 ``cache_read`` 放在 ``usage_metadata.input_token_details``
    下，而 ``input_tokens`` 在 ``usage_metadata`` 顶层）。只在首个成功的映射里
    找，会稳定地读到 0——那正好会把「命中很好」误报成「一次都没命中」。
    """
    for mapping in mappings:
        number = _read_counter(mapping, keys)
        if number is not None:
            return number
    return None


def _usage_from_mapping(mapping: Mapping[str, Any]) -> TokenUsage | None:
    """从一份映射里解析用量；两个计数都没找到时返回 ``None``。

    WHY 不在这里读缓存计数：它们可能不在同一层，由 ``normalize_usage``
    跨候选映射补齐，保证「主口径决定这是不是一次有效用量」与「缓存明细从
    哪里取」两件事不互相牵扯。
    """
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

    mappings = _candidate_mappings(payload)

    chosen: TokenUsage | None = None
    for mapping in mappings:
        usage = _usage_from_mapping(mapping)
        if usage is not None:
            chosen = usage
            break
    if chosen is None:
        return None

    # WHY 缓存计数要跨全部候选映射补：它们与 input/output 可能不在同一层
    # （见 ``_read_counter_across``）。补不上就保持 0，语义是「provider 未上报」。
    cache_hit = _read_counter_across(mappings, _CACHE_HIT_KEYS)
    cache_miss = _read_counter_across(mappings, _CACHE_MISS_KEYS)
    if cache_hit is None and cache_miss is None:
        return chosen
    return replace(
        chosen,
        cache_hit_tokens=cache_hit if cache_hit is not None else chosen.cache_hit_tokens,
        cache_miss_tokens=cache_miss if cache_miss is not None else chosen.cache_miss_tokens,
    )


def _sum_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    """逐字段相加两份用量。

    WHY 单独成函数而不是在累加器里手写两遍：采集字段会随口径增加（缓存
    命中/未命中就是后加的），逐处手写迟早漏掉一个——漏掉的表现是「那个数字
    恒为 0」，既不报错也不易察觉。
    """
    return TokenUsage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        cache_hit_tokens=left.cache_hit_tokens + right.cache_hit_tokens,
        cache_miss_tokens=left.cache_miss_tokens + right.cache_miss_tokens,
    )


class UsageAccumulator:
    """累计一轮运行中**逐次模型调用**的用量，并保留分段明细。

    WHY 按调用分段（2026-09-24 改）：此前的判据是「计数回退即新调用」，但一次运行
    内多次模型调用的 prompt 是**递增**的（每次都带上更多历史），递增不触发回退，
    于是后一次调用直接覆盖前一次——该轮前面调用的用量全部丢失。实测表现：会话
    df0b66e5… 的第 1 轮至少有两次模型调用（检查点里出现两处 ``tool_calls``），
    用量表却只有一行。分段信号因此改用消息 ``id``：同一响应的所有分片共享同一个
    id，id 变化必然是新调用。

    WHY 保留「计数回退」判据作为兜底：部分 provider / 网关的分片没有 id，此时
    无法按 id 分段，退回旧判据——精度差一些，但不能因为分段信号缺失而丢掉整轮。

    WHY 逐片相加仍然禁止：同一次调用内各分片携带的是**累计快照**（OpenAI 兼容端
    在最后一个 chunk 给出全量，Anthropic 在 ``message_delta`` 里给累计输出），
    逐片相加会把一次调用放大成几十倍。所以规则是：**同 id 内取最后一片，id 变化
    才结算相加**。
    """

    def __init__(self) -> None:
        self._settled: list[TokenUsage] = []
        self._current: TokenUsage | None = None
        self._current_call_id: str | None = None

    def add(self, usage: TokenUsage | None, *, call_id: str | None = None) -> bool:
        """并入一份用量快照，并按调用分段。

        Args:
            usage: 归一化后的用量；``None`` 表示本次没有用量，直接忽略。
            call_id: 该分片所属响应的 id（LangChain 消息的 ``id`` 字段），同一
                响应的所有分片共享同一 id；``None`` 表示无法获得。

        Returns:
            是否确实并入了数据（``False`` 表示该分片没有用量字段）。
        """
        if usage is None:
            return False

        if self._current is None:
            self._start(usage, call_id)
            return True

        # WHY 分段以 id 为最高优先级：两侧都有 id 时，「id 不同」是唯一可靠的
        # 新调用信号；此时无论计数是否回退都应分段——上下文增长时计数只会涨。
        if (
            call_id is not None
            and self._current_call_id is not None
            and call_id != self._current_call_id
        ):
            self._settle()
            self._start(usage, call_id)
            return True

        if call_id is not None and call_id == self._current_call_id:
            # 同一次调用内的后续分片：取较新的（更大的）累计值
            self._current = usage
            return True

        # 至少一侧拿不到 id，退回计数回退推断。
        # WHY 判据只用 prompt / total 而不看缓存计数：缓存命中数在一次调用内
        # 可能先为 0、后由 provider 补齐，把它纳入回退判据会把同一调用误判成
        # 两次调用，结果是重复计费式的放大。
        is_new_call = (
            usage.prompt_tokens < self._current.prompt_tokens
            or usage.total_tokens < self._current.total_tokens
        )
        if is_new_call:
            self._settle()
            self._start(usage, call_id)
            return True

        # 同一次调用内的后续分片：取较新的（更大的）累计值
        self._current = usage
        return True

    def add_raw(self, payload: Any) -> bool:
        """归一化并入一份原始载荷（消息对象或响应字典）。

        WHY 从消息对象上取 ``id`` 作为分段信号：LangChain 给同一响应的所有分片
        分配同一个 id，这是「同一次调用」最可靠的判据——而 prompt 计数在多次
        调用之间是递增的，靠数值回退会把同一轮的多次调用误并成一次。
        """
        call_id = getattr(payload, "id", None)
        if not isinstance(call_id, str) or not call_id:
            call_id = None
        return self.add(normalize_usage(payload), call_id=call_id)

    def _start(self, usage: TokenUsage, call_id: str | None) -> None:
        self._current = usage
        self._current_call_id = call_id

    def _settle(self) -> None:
        if self._current is not None:
            self._settled.append(self._current)
        self._current = None
        self._current_call_id = None

    @property
    def calls(self) -> list[TokenUsage]:
        """逐次模型调用的用量快照（按发生顺序；最后一段可能尚未结束）。

        WHY 需要逐次而不是只有总和：缓存命中率的趋势只能按调用看——一轮运行内
        首次调用通常命中率低（新内容）、后续调用命中历史，只留总和会把这层
        结构整个抹平。
        """
        if self._current is None:
            return list(self._settled)
        return [*self._settled, self._current]

    @property
    def total(self) -> TokenUsage:
        """本轮**全部调用**的用量之和；从未取到用量时为零值。

        WHY 是求和而不是「最后一次快照」：同一次调用内的分片是累计快照（取最后
        一片即可），但不同调用之间的计数各自从零开始，必须相加才是本轮的真实
        消耗。此前按「最后一次快照」处理，会系统性低估多调用运行的用量。
        """
        total = TokenUsage(0, 0)
        for usage in self._settled:
            total = _sum_usage(total, usage)
        if self._current is not None:
            total = _sum_usage(total, self._current)
        return total

    @property
    def empty(self) -> bool:
        """是否从未取到任何用量。"""
        return not self._settled and self._current is None


def approximate_tokens(
    messages: Iterable[Any], *, tools: Iterable[Any] | None = None
) -> int:
    """按上游近似口径估算消息的 token 数（4 字符 ≈ 1 token）。

    WHY 不直接用上游的 ``use_usage_metadata_scaling=True``：该缩放被钳制在
    ``[1.0, 1.25]``（见 ``langchain_core.messages.utils``），而中文在 4 字符折算
    下被低估约 2.5–4 倍——1.25 的上限校不动这个量级，等于没校。因此本模块自建
    两段式：先用本函数取基数，再由调用方用真实用量乘上缩放系数（见
    :func:`calibrated_tokens` 与 :func:`usage_scale_factor`）。

    Args:
        messages: 消息序列（LangChain 消息对象或等价字典）。
        tools: 参与计数的工具 schema；其 JSON 长度会一并计入。

    Returns:
        估算 token 数（向上取整）。
    """
    return count_tokens_approximately(messages, tools=list(tools) if tools else None)


def calibrated_tokens(
    messages: Iterable[Any], *, scale: float, tools: Iterable[Any] | None = None
) -> int:
    """近似计数乘上缩放系数，得到校准后的估算值。

    Args:
        messages: 消息序列。
        scale: 缩放系数，由一次「真实用量 ÷ 同口径近似值」得出（见
            :func:`usage_scale_factor`）；应 >= 1.0，中文场景通常在 2.5–4 之间。
        tools: 参与计数的工具 schema。

    Returns:
        校准后的估算 token 数（向上取整）。

    Raises:
        ValueError: ``scale`` 不是非负数。
    """
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale < 0:
        raise ValueError(f"scale 必须是非负数，实际：{scale!r}")
    return math.ceil(approximate_tokens(messages, tools=tools) * scale)


def usage_scale_factor(real_tokens: int, approx_tokens: int) -> float:
    """由一次真实用量与同口径近似值计算缩放系数。

    Args:
        real_tokens: provider 实报的 prompt token 数。
        approx_tokens: 同一份消息在 :func:`approximate_tokens` 下的估算值。

    Returns:
        缩放系数（>= 1.0）；估算值非正时回退为 1.0——此时没有可信的校准依据，
        直接用近似值比用一个错误系数放大更安全。

    Raises:
        ValueError: ``real_tokens`` 不是非负整数。
    """
    if not isinstance(real_tokens, int) or isinstance(real_tokens, bool):
        raise ValueError(f"real_tokens 必须是整数，实际：{type(real_tokens).__name__}")
    if real_tokens < 0:
        raise ValueError(f"real_tokens 不能为负数，实际：{real_tokens}")
    if approx_tokens <= 0:
        return 1.0
    return max(1.0, real_tokens / approx_tokens)


__all__ = [
    "TokenUsage",
    "UsageAccumulator",
    "approximate_tokens",
    "calibrated_tokens",
    "cache_hit_rate",
    "normalize_usage",
    "usage_scale_factor",
]
