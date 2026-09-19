"""认证相关审计事件的统一产出点。

WHY 集中在一处：审计字段的含义必须全站一致（谁、做了什么、结果如何）。
如果各路由各自拼装字段，会出现同一个 ``outcome`` 在不同端点取值不同的漂移，
使审计日志失去可检索性。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from application.ports import AuditSink
from interfaces.web.auth.utils import client_ip, user_agent

logger = logging.getLogger(__name__)

_missing_store_warned = False
"""是否已就「审计存储未装配」告警过一次。

WHY 只告警一次：本函数在认证路径上逐个请求被调用，每次事件都打一行 WARNING，等于给
扫描器一个刷日志的开关——而那正是最需要看清日志的场景。

WHY 不再像以前那样完全静默：装配漏项时它会悄悄丢掉**全部**认证审计（登录成功/失败、
权限拒绝、logout、key 增删），而这类缺陷在界面上只表现为「审计面板是空的」，很容易被
当成「还没产生事件」。事实上本仓的 ``app.state.audit_store`` 漏铺就是这样被藏住的：
直到有人点开审计面板撞上 500 才暴露。
"""


async def log_auth_event(
    request: Request,
    *,
    event_type: str,
    actor_id: str,
    outcome: str,
    action: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """记录认证相关审计事件。

    WHY 不向上抛异常：审计写入失败不应导致认证流程失败——否则存储故障会
    直接演变成「全站无法登录」。失败会记入应用日志以便另行告警。

    Args:
        request: 当前请求，用于提取 IP 与 User-Agent。
        event_type: 事件类型，如 ``login_success`` / ``apikey_auth_failure``。
        actor_id: 行为主体标识，未知时传 ``"unknown"``。
        outcome: 结果，通常为 ``success`` 或 ``failure``。
        action: 具体动作标识，可选。
        details: 附加信息，会以 JSON 落库。
    """
    global _missing_store_warned

    audit_store: AuditSink | None = getattr(request.app.state, "audit_store", None)
    if audit_store is None:
        # 审计存储未装配（例如只挂了部分路由的极简部署或测试替身）：本函数不因此失败，
        # 但必须让「有一条持续的审计缺口」这件事可见——只提示一次，见模块级说明。
        if not _missing_store_warned:
            _missing_store_warned = True
            logger.warning(
                "审计存储未装配：认证相关审计（登录/权限拒绝/logout/API Key 增删）将被丢弃，"
                "本次仅提示一次。请检查 interfaces/web/app.py 的 lifespan 是否漏铺了 app.state.audit_store"
            )
        return
    try:
        await audit_store.log(
            event_type=event_type,
            actor_id=actor_id,
            action=action,
            outcome=outcome,
            ip=client_ip(request),
            user_agent=user_agent(request),
            details=details,
        )
    except Exception:
        logger.exception("审计事件写入失败：event_type=%s actor=%s", event_type, actor_id)
