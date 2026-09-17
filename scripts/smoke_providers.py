"""多 provider 冒烟验证：对每个已注册模型做一次最小真实调用。

WHY 需要它：``llm/registry.py`` 的构造路径会捕获 ``init_chat_model`` 的异常，
但「构造成功」不等于「调得通」——API 地址写错、密钥失效、provider 包版本与
kwargs 不兼容，都只在真正发出一次请求时才暴露。本脚本把「逐个 provider 真的
能回一句话」变成一条可重复执行的命令，而不是等某个模型的首次真实使用去发现。

WHY 跳过与失败必须分开报：一台只配了 DeepSeek 的机器永远存在「跳过」。
把它算作失败，这条命令会天天红、很快没人再看；把它算作通过，
「到底哪些 provider 从没被验证过」又彻底不可见。

退出码（三态）：
    0  全通过——候选 provider 全部已配置且调用成功
    2  有跳过——没有失败，但有 provider 因未配置密钥/地址被跳过
    1  有失败——至少一个 provider 配置齐全却调不通
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
# WHY 强制 UTF-8 输出：Windows 控制台默认 GBK，而 provider 的报错文本常含
# 非 GBK 字符，print 会抛 UnicodeEncodeError，把验证结论变成一条假失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent.profiles import ensure_profiles_registered  # noqa: E402
from config import AppConfig  # noqa: E402
from llm.registry import (  # noqa: E402
    ModelRegistry,
    ModelSpec,
    build_default_registry,
    default_specs,
)

logger = logging.getLogger(__name__)

EXIT_ALL_OK = 0
EXIT_HAS_FAILURE = 1
EXIT_HAS_SKIP = 2

_PROBE_PROMPT = "ping"
"""最小调用的问题。

WHY 只要求「回任何非空内容」而不校验措辞：校验措辞会变成一条对模型版本脆弱的
断言——同一 provider 换个小版本就可能改掉措辞，让冒烟结论失真。
"""

_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")
"""兜底的密钥形态匹配：provider 报错常回显 ``sk-`` 开头的片段。

WHY 需要兜底：个别 SDK 会把密钥中段自己打码（实测 OpenAI 回显
``sk-inval*******************7890``），此时按明文替换同样无从匹配——
两层都留着才覆盖得全。
"""

_CONFIGURED_SECRETS: set[str] = set()
"""本次进程内配置过的密钥明文，用于**按值打码**。

WHY 按值打码优先于按形态打码：自建端点或非标准发放的密钥不一定带 ``sk-``
前缀，只按形态匹配必然漏；而已知明文一定打得掉。脚本单次运行、只在 ``main``
里写一次，故用模块级集合而不层层透传。
"""


class Outcome(NamedTuple):
    """单个 provider 的验证结论。"""

    name: str
    status: str
    """``ok`` / ``skip`` / ``fail``。"""

    detail: str


def _redact(text: str) -> str:
    """打掉输出里的密钥：先按已知明文，再按 ``sk-`` 形态兜底。

    WHY 必须打码：本脚本的输出经常被贴进 issue 或聊天窗口做排障，
    把密钥原样带出去等于让一次调试变成一次泄露。
    """
    result = text
    for secret in _CONFIGURED_SECRETS:
        result = result.replace(secret, "***")
    return _SECRET_PATTERN.sub("sk-***", result)


def _remember_secrets(config: AppConfig) -> None:
    """记下本次配置里的 LLM 密钥明文，供 ``_redact`` 使用。"""
    _CONFIGURED_SECRETS.update(
        value.strip()
        for value in (config.deepseek_api_key, config.openai_api_key, config.anthropic_api_key)
        if isinstance(value, str) and value.strip()
    )


def _text_of(message: Any) -> str:
    """从模型回复里取出可读文本。

    WHY 需要兼容两种形态：多模态 provider 的 ``content`` 可能是内容块列表而非
    字符串，直接 ``str()`` 得到的是 Python 字面量而不是回答本身。
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return " ".join(parts).strip()
    return "" if content is None else str(content).strip()


async def _check_one(spec: ModelSpec, registry: ModelRegistry, available: set[str]) -> Outcome:
    """对一个候选 provider 做「确认注册 → 静态探测 → 构造 → 最小调用」。

    Args:
        spec: 候选模型定义。
        registry: 已注册的模型注册表。
        available: 本次真正注册成功的别名集合。

    Returns:
        结论；``status`` 为 ``ok`` / ``skip`` / ``fail``。
    """
    if spec.name not in available:
        env_name = spec.api_key_env or spec.base_url_env
        return Outcome(spec.name, "skip", f"未配置 {env_name}，未注册")

    probe = registry.probe_config(spec.name)
    if not probe.ok:
        # WHY 判失败而不是跳过：能走到这里说明注册规则已认定它「可注册」，
        # 而静态探测却说不自洽——二者分叉是缺陷，不是「没配」。
        return Outcome(spec.name, "fail", f"注册与配置分叉：{_redact(probe.detail)}")

    try:
        llm = registry.get(spec.name)
    except Exception as exc:  # noqa: BLE001 - 逐个记录，不中断其余 provider 的验证
        logger.debug("构造失败", exc_info=True)
        return Outcome(spec.name, "fail", f"构造失败：{_redact(str(exc))}")

    try:
        response = await llm.ainvoke(_PROBE_PROMPT)
    except Exception as exc:  # noqa: BLE001 - 同上
        logger.debug("调用失败", exc_info=True)
        return Outcome(spec.name, "fail", f"调用失败：{type(exc).__name__}: {_redact(str(exc))}")

    text = _text_of(response)
    if not text:
        return Outcome(spec.name, "fail", "调用返回空内容")

    return Outcome(spec.name, "ok", f"{spec.provider}:{spec.model} → {_redact(text[:40])!r}")


async def _check_all(registry: ModelRegistry, candidates: list[ModelSpec]) -> list[Outcome]:
    """顺序验证全部候选 provider。

    WHY 串行而不并发：本脚本诊断的是「网络与凭据」，并发会把 provider 侧的限流
    错误与「压根连不上」混在一起，输出顺序也不再可读。
    """
    available = set(registry.names())
    return [await _check_one(spec, registry, available) for spec in candidates]


def _print_outcomes(outcomes: list[Outcome]) -> None:
    """打印逐项结论。"""
    width = max(len(item.name) for item in outcomes)
    print("\n--- 结论 ---")
    for item in outcomes:
        print(f"  {item.name:<{width}}  {item.status.upper():<5} {item.detail}")


def main() -> int:
    """执行验证并返回三态退出码（取值见模块 docstring）。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    # WHY 先注册 profile：这是 build_default_registry 的前置条件（见其 docstring），
    # 顺序与 agent.graph.get_registry 的装配路径一致——否则这里验证的模型与真实
    # 运行时的模型不是同一个东西。
    ensure_profiles_registered()

    try:
        config = AppConfig.load()
    except Exception:  # noqa: BLE001 - 配置不可用是明确失败，不该把 pydantic 原始栈糊给用户
        logger.exception("配置加载失败，无法进行 provider 冒烟")
        return EXIT_HAS_FAILURE

    _remember_secrets(config)
    candidates = default_specs(config)
    registry = build_default_registry(config)

    print(f"[cfg] default_model={config.default_model}")
    print(f"      候选 provider={[spec.name for spec in candidates]}")
    print(f"      本次已注册={registry.names()}")

    outcomes = asyncio.run(_check_all(registry, candidates))
    _print_outcomes(outcomes)

    ok = sum(1 for item in outcomes if item.status == "ok")
    skipped = sum(1 for item in outcomes if item.status == "skip")
    failed = sum(1 for item in outcomes if item.status == "fail")
    print(f"\n通过 {ok} / 跳过 {skipped} / 失败 {failed}")

    if failed:
        return EXIT_HAS_FAILURE
    return EXIT_HAS_SKIP if skipped else EXIT_ALL_OK


if __name__ == "__main__":
    sys.exit(main())
