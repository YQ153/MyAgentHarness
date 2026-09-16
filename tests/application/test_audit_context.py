"""审计请求上下文（IP / UA）的回归测试。

WHY 单独成文件：``contextvars`` 的行为错误（串上下文、泄漏到后续协程）
不会在单次调用中暴露，只在并发或复用场景下出现，必须有针对性的用例。
"""

from __future__ import annotations

import asyncio

import pytest

from application.audit_context import (
    MAX_IP_CHARS,
    MAX_USER_AGENT_CHARS,
    RequestContext,
    audit_client_info,
    bind_request_context,
    current_request_context,
    request_context,
    reset_request_context,
)


# ------------------------------------------------------------------ 基础语义


def test_default_context_is_empty():
    context = current_request_context()

    assert context == RequestContext()
    # 未绑定时审计字段必须是 None：空串会让「未知来源」与「空值」混为一谈
    assert audit_client_info() == (None, None)


def test_bind_then_reset_restores_previous_value():
    first = bind_request_context(ip="10.0.0.1", user_agent="agent-a")
    assert current_request_context().ip == "10.0.0.1"

    second = bind_request_context(ip="10.0.0.2", user_agent="agent-b")
    assert current_request_context().ip == "10.0.0.2"

    reset_request_context(second)
    assert current_request_context().ip == "10.0.0.1"

    reset_request_context(first)
    assert current_request_context() == RequestContext()


def test_context_manager_rolls_back_on_exception():
    with pytest.raises(RuntimeError):
        with request_context(ip="203.0.113.5", user_agent="exploding-agent"):
            raise RuntimeError("下游炸了")

    assert current_request_context() == RequestContext()


def test_reset_rejects_none_token():
    with pytest.raises(ValueError):
        reset_request_context(None)


# ------------------------------------------------------------------ 输入校验


@pytest.mark.parametrize("bad_ip", [123, object(), 1.5])
def test_non_string_ip_is_ignored(bad_ip):
    with request_context(ip=bad_ip, user_agent="ua"):
        assert current_request_context().ip == ""
        assert current_request_context().user_agent == "ua"


def test_none_values_treated_as_unknown():
    with request_context(ip=None, user_agent=None):
        assert current_request_context() == RequestContext()


def test_overlong_fields_are_truncated():
    long_ua = "x" * (MAX_USER_AGENT_CHARS + 500)
    long_ip = "1" * (MAX_IP_CHARS + 100)

    with request_context(ip=long_ip, user_agent=long_ua):
        context = current_request_context()
        assert len(context.ip) == MAX_IP_CHARS
        assert len(context.user_agent) == MAX_USER_AGENT_CHARS


def test_values_are_trimmed():
    with request_context(ip="  10.1.1.1  ", user_agent="  curl/8.0 "):
        assert current_request_context().ip == "10.1.1.1"
        assert current_request_context().user_agent == "curl/8.0"


# ------------------------------------------------------------------ 并发隔离


async def test_concurrent_tasks_see_their_own_context():
    """WHY 本用例是这套机制的核心契约：同一进程里并发处理两个请求，
    任何串扰都会让审计把 A 的操作记在 B 的 IP 下。"""

    async def worker(ip: str, delay: float) -> tuple[str, str | None]:
        with request_context(ip=ip, user_agent=f"agent-{ip}"):
            await asyncio.sleep(delay)
            value, ua = audit_client_info()
            return value or "", ua

    first, second = await asyncio.gather(
        worker("198.51.100.1", 0.02),
        worker("198.51.100.2", 0.01),
    )

    assert first == ("198.51.100.1", "agent-198.51.100.1")
    assert second == ("198.51.100.2", "agent-198.51.100.2")
    # 子任务的绑定不得反向影响调用方
    assert current_request_context() == RequestContext()
