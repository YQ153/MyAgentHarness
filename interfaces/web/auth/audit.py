"""认证相关审计事件的统一产出点（Web 形态）。

WHY 集中在一处：审计字段的含义必须全站一致（谁、做了什么、结果如何）。
如果各路由各自拼装字段，会出现同一个 ``outcome`` 在不同端点取值不同的漂移，
使审计日志失去可检索性。

WHY 写入本身交给 ``application.api_key_auth.write_auth_audit``：那条「审计失败不影响
认证，但必须让『有一条持续的审计缺口』可见」的约定，与 API Key 认证路径共用一份——
CLI 没有 ``Request``，写不出第二份而只能复制一份，复制就会漂移。
本模块因此只剩一件事：把 ``Request`` 翻译成 ip 与 User-Agent 两个字段。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from application.api_key_auth import write_auth_audit
from interfaces.web.auth.utils import client_ip, user_agent


async def log_auth_event(
    request: Request,
    *,
    event_type: str,
    actor_id: str,
    outcome: str,
    action: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """记录认证相关审计事件（从请求里取 IP 与 User-Agent）。

    Args:
        request: 当前请求，用于提取 IP 与 User-Agent。
        event_type: 事件类型，如 ``login_success`` / ``apikey_auth_failure``。
        actor_id: 行为主体标识，未知时传 ``"unknown"``。
        outcome: 结果，通常为 ``success`` 或 ``failure``。
        action: 具体动作标识，可选。
        details: 附加信息，会以 JSON 落库。
    """
    await write_auth_audit(
        getattr(request.app.state, "audit_store", None),
        event_type=event_type,
        actor_id=actor_id,
        outcome=outcome,
        action=action,
        details=details,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )
