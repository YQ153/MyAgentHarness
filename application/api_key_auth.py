"""API Key 校验：CLI 与 Web 共用的唯一实现。

WHY 必须共用：同一件事（比对 dev key → 查存储 → 构造 Principal）此前在两个入口各写一遍。
两份实现**都能登录**，所以功能测试发现不了差异——差异在别处，且都是看不见的那种：

- **审计**：Web 侧成功与失败各落一条，CLI 侧一条都不落（``bootstrap/core.py`` 的 docstring
  写明了要避免「Web 有审计、CLI 无审计」，装配收敛了，认证语义没有）；
- **主体字段**：同一个 dev key 换来的 ``Principal``，Web 侧带 ``scopes``、CLI 侧不带
  （``scopes`` 由 ``/auth/me`` 回给浏览器，日后若让它参与权限判断，CLI 侧会静默少权限）。

把两份并排看才发现的问题，说明它不该有两份。

WHY 放在 ``application``：校验需要 ``APIKeyRepository`` 与 ``Principal``，两者都在应用层；
而调用方一个在 ``interfaces/web``（有 ``Request``）、一个在 ``interfaces/cli``（没有）——
放进任何一侧都会让另一侧绕路。

WHY 审计**内容**由本模块决定、审计**写入**由调用方触发：事件类型 / actor / details 是
"这条校验结果意味着什么"，属于同一条业务规则，写两份必然漂移（``interfaces/web/auth/audit.py``
的 docstring 正是在说这件事）；而 ``ip`` / ``user_agent`` 只有 HTTP 形态才有，属传输细节。
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from application.ports import APIKeyRepository, AuditSink
from application.principal import ROLE_PERMISSIONS, Principal

logger = logging.getLogger(__name__)

_AUTH_ACTION = "validate"
"""认证动作标识。

WHY 定义在这里而不是让调用方各自传：两个入口必须写同一个值，
否则按 action 检索审计时会漏掉一半（而"漏一半"在面板上看起来像"那类事件没发生过"）。
"""

_ENTRY_KEY = "entry"
"""审计 details 里标记"从哪个入口来的"的键名。

WHY 不叫 ``source``：``source`` 在 dev key 场景已被占用（``{"source": "env_dev_key"}``），
复用它会把那个含义覆盖掉，于是"用了环境变量里的应急密钥"这条信息消失。
"""

_missing_sink_warned = False
"""是否已就「审计存储未装配」告警过一次。

WHY 只告警一次：本模块在认证路径上逐个请求被调用，每次事件都打一行 WARNING，等于给
扫描器一个刷日志的开关——而那正是最需要看清日志的场景。

WHY 不再完全静默：装配漏项时会悄悄丢掉**全部**认证审计（登录成功/失败、权限拒绝、
API Key 增删），而这类缺陷在界面上只表现为「审计面板是空的」，很容易被当成
「还没产生事件」。本仓的 ``app.state.audit_store`` 漏铺就是这样被藏住的。
"""


@dataclass(frozen=True)
class ApiKeyAuthResult:
    """一次 API Key 校验的结果：主体（失败时为 ``None``）与该记的审计内容。

    WHY 把审计内容一并返回、而不是只返回主体：调用方需要知道"这次是成功还是失败、
    失败原因是什么"才能写出正确的审计；若只给主体，失败路径上信息就丢了，
    调用方只能自己重判一次——那正是要消灭的第二份实现。
    """

    principal: Principal | None
    event_type: str
    actor_id: str
    outcome: str
    details: dict[str, Any] = field(default_factory=dict)


async def validate_api_key(
    api_key: str,
    *,
    dev_key: str,
    store: APIKeyRepository | None = None,
) -> ApiKeyAuthResult:
    """校验 API Key，返回结果（失败用 ``principal=None`` 表达，**不抛异常**）。

    WHY 失败不抛异常：两个入口对失败的处理**不同**——Web 按未认证处理（401），
    CLI 打印提示并退出 2。异常类型会把这个差异硬编码进本模块，
    于是两个入口又得各写一层翻译；返回 ``None`` 让差异留在各自的适配器里。

    Args:
        api_key: 待校验的凭据。
        dev_key: 配置里的应急密钥（``AUTH_API_KEY_DEV``）；空串表示未启用。
        store: API Key 存储；``None`` 表示未装配。

    Returns:
        校验结果。``principal`` 为 ``None`` 时，``event_type`` / ``details`` 说明失败原因。

    Raises:
        ValueError: ``api_key`` 或 ``dev_key`` 不是字符串（类型错误属于调用方的问题，
            不是一次认证失败）。

    Note:
        WHY 空串与全空白**不**当成参数错误：它们只是匹配不上，走寻常的失败路径即可。
        Web 侧的凭据直接来自请求头，把"形态不对"做成异常，等于让一个畸形的头
        把认证路径变成 500——而它的正确结果是 401。
    """
    if not isinstance(api_key, str):
        raise ValueError(f"api_key 必须是字符串，实际：{type(api_key).__name__}")
    if not isinstance(dev_key, str):
        raise ValueError(f"dev_key 必须是字符串，实际：{type(dev_key).__name__}")

    # WHY 先判 isascii：``secrets.compare_digest`` 对非 ASCII 的 str 直接抛 TypeError，
    # 而 ``api_key`` 在 Web 侧来自请求头（不受信输入）——不判就等于让一个非 ASCII 头
    # 把 500 打出来，而不是按"凭据不匹配"处理。
    if dev_key:
        if not dev_key.isascii():
            # dev_key 是运营方填的配置，不是攻击面：静默跳过比对会让"应急入口怎么都进不去"
            # 变成一个查不出原因的现象——比对函数不报错，只是永远不匹配。
            logger.warning(
                "AUTH_API_KEY_DEV 含非 ASCII 字符，该应急密钥永远不会匹配：请改用 ASCII 密钥"
            )
        elif api_key.isascii() and secrets.compare_digest(api_key, dev_key):
            # WHY 应急密钥的主体与普通 Key 走同一套字段口径：同一个主体在两个入口必须
            # 长得一样，否则「同一个 key 在网页上是管理员、在命令行上不是」——
            # 而 ``/auth/me`` 与 ``has_scope`` 都读这些字段。
            return ApiKeyAuthResult(
                principal=Principal(
                    user_id="apikey:dev",
                    display_name="dev",
                    role="admin",
                    scopes=frozenset(ROLE_PERMISSIONS["admin"]),
                    auth_method="apikey",
                ),
                event_type="apikey_auth_success",
                actor_id="apikey:dev",
                outcome="success",
                details={"source": "env_dev_key"},
            )

    if store is None:
        # WHY 区分"存储没装配"与"凭据无效"：两者的处置方式完全不同（前者是部署问题、
        # 后者要怀疑凭据），而它们在响应上可能长得一样（都是 401）。
        return ApiKeyAuthResult(
            principal=None,
            event_type="apikey_auth_failure",
            actor_id="unknown",
            outcome="failure",
            details={"reason": "store_unavailable"},
        )

    record = await store.validate(api_key)
    if record is None:
        return ApiKeyAuthResult(
            principal=None,
            event_type="apikey_auth_failure",
            actor_id="unknown",
            outcome="failure",
            details={"reason": "invalid_or_revoked"},
        )

    actor_id = f"apikey:{record['key_id']}"
    return ApiKeyAuthResult(
        principal=Principal(
            user_id=actor_id,
            display_name=f"API Key {record.get('key_prefix', '')}...",
            role=record["role"],
            scopes=frozenset((record.get("scopes") or "").split()),
            auth_method="apikey",
        ),
        event_type="apikey_auth_success",
        actor_id=actor_id,
        outcome="success",
        details={"key_id": record["key_id"], "role": record["role"]},
    )


async def write_auth_audit(
    sink: AuditSink | None,
    *,
    event_type: str,
    actor_id: str,
    outcome: str,
    action: str | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """把一条认证审计写进 ``sink``；**绝不向上抛异常**。

    WHY 不抛：审计写入失败不应导致认证失败——否则存储故障会直接演变成「全站无法登录」。
    失败会记进应用日志以便另行告警。

    Args:
        sink: 审计存储；``None`` 表示未装配（只告警一次，不失败）。
        event_type: 事件类型，如 ``apikey_auth_failure``。
        actor_id: 行为主体标识，未知时传 ``"unknown"``。
        outcome: 结果，通常为 ``success`` / ``failure``。
        action: 具体动作标识，可选。
        details: 附加信息，会以 JSON 落库。
        ip: 来源地址；非 HTTP 形态（CLI）传 ``None``——"没有这个字段"与"取不到"是两回事，
            填空串会让审计面板上出现一个看起来像异常的空白值。
        user_agent: 客户端标识，同上。
    """
    global _missing_sink_warned

    if sink is None:
        if not _missing_sink_warned:
            _missing_sink_warned = True
            logger.warning(
                "审计存储未装配：认证相关审计（登录/权限拒绝/API Key 增删）将被丢弃，"
                "本次仅提示一次。请检查装配是否漏铺审计存储"
                "（Web 形态见 interfaces/web/app.py 的 lifespan）。"
            )
        return

    try:
        await sink.log(
            event_type=event_type,
            actor_id=actor_id,
            action=action,
            outcome=outcome,
            ip=ip,
            user_agent=user_agent,
            details=details,
        )
    except Exception:
        logger.exception("审计事件写入失败：event_type=%s actor=%s", event_type, actor_id)


async def record_api_key_auth(
    sink: AuditSink | None,
    result: ApiKeyAuthResult,
    *,
    entry: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """把一次 API Key 校验的结果落进审计。

    Args:
        sink: 审计存储；``None`` 表示未装配。
        result: :func:`validate_api_key` 的返回值。
        entry: 入口标识（如 ``"cli"``）。

            WHY 只有非 HTTP 入口需要传：Web 侧的审计载荷要逐字保持既有形态
            （``entry`` 是本次新增的键），而 CLI 侧的审计如果不标入口，
            在面板上与一次 HTTP 请求长得完全一样——看不出"这条来自本机命令行"。
        ip: 来源地址。
        user_agent: 客户端标识。
    """
    details = dict(result.details)
    if entry:
        details[_ENTRY_KEY] = entry
    await write_auth_audit(
        sink,
        event_type=result.event_type,
        actor_id=result.actor_id,
        outcome=result.outcome,
        action=_AUTH_ACTION,
        details=details,
        ip=ip,
        user_agent=user_agent,
    )


__all__ = [
    "ApiKeyAuthResult",
    "record_api_key_auth",
    "validate_api_key",
    "write_auth_audit",
]
