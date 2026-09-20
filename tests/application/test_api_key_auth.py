"""共用 API Key 校验的行为规格。

WHY 单独成文件：这一份实现同时服务 CLI 与 Web 两个入口。收敛之前它们各写一份，
**两份都能登录**——所以差异不在"能不能进"，而在审计内容与主体字段上，
功能测试看不见。把规格钉在这里，是让"两个入口一致"这件事第一次变成可断言的事实。

WHY 断言到 details 而不只断言成功/失败：审计的价值全在字段里（成功来自应急密钥还是
数据库 Key、失败是存储没装配还是凭据无效）；只断言 principal 不为 None，
等于把"审计记了什么"整个放掉。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from application import api_key_auth
from application.api_key_auth import (
    ApiKeyAuthResult,
    record_api_key_auth,
    validate_api_key,
    write_auth_audit,
)
from application.principal import ROLE_PERMISSIONS

_DEV_KEY = "dev-secret"


class _StubKeyStore:
    """``APIKeyRepository`` 替身：返回预置记录并记录被查询过的密钥。"""

    def __init__(self, record: dict[str, Any] | None = None) -> None:
        self._record = record
        self.validated: list[str] = []

    async def validate(self, key: str) -> dict[str, Any] | None:
        self.validated.append(key)
        return self._record


class _RecordingSink:
    """``AuditSink`` 替身：记录字段。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def log(self, **fields: Any) -> None:
        self.calls.append(fields)


class _BrokenSink:
    async def log(self, **fields: Any) -> None:
        raise RuntimeError("审计库炸了")


# ------------------------------------------------------------------ 校验


async def test_dev_key_wins_and_never_reaches_the_store() -> None:
    """应急密钥命中后不再查库。

    WHY 值得钉：多查一次不只是浪费——库侧若有一条同值记录，就会夺走应急入口的语义
    （"配在 .env 里那把一定能进"）。
    """
    store = _StubKeyStore({"key_id": "k1", "key_prefix": "abcd", "role": "member"})

    result = await validate_api_key(_DEV_KEY, dev_key=_DEV_KEY, store=store)

    assert result.principal is not None
    assert result.principal.user_id == "apikey:dev"
    assert store.validated == []
    assert result.details == {"source": "env_dev_key"}


async def test_dev_key_principal_carries_admin_scopes() -> None:
    """应急密钥的主体必须与 Web 侧逐字一致（含 scopes）。

    WHY：同一把密钥在两个入口换来的主体此前不同——Web 侧带 scopes、CLI 侧不带。
    scopes 会经 ``/auth/me`` 回给浏览器，也是 ``has_scope`` 唯一的输入，
    一旦它参与权限判断，少 scopes 的那一侧会静默失去权限。
    """
    result = await validate_api_key(_DEV_KEY, dev_key=_DEV_KEY)

    assert result.principal is not None
    assert result.principal.scopes == frozenset(ROLE_PERMISSIONS["admin"])


async def test_valid_store_record_builds_principal_from_the_record() -> None:
    store = _StubKeyStore(
        {
            "key_id": "k1",
            "key_prefix": "abcd",
            "role": "member",
            "scopes": "usage:read audit:read",
        }
    )

    result = await validate_api_key("key-1", dev_key="", store=store)

    assert result.principal is not None
    assert result.principal.user_id == "apikey:k1"
    assert result.principal.display_name == "API Key abcd..."
    assert result.principal.role == "member"
    assert result.principal.scopes == frozenset({"usage:read", "audit:read"})
    assert result.event_type == "apikey_auth_success"
    assert result.details == {"key_id": "k1", "role": "member"}


async def test_missing_store_and_unknown_key_are_distinguishable() -> None:
    """两种失败必须能被分开。

    WHY：它们的审计原因不同、处置方式也不同（部署问题 vs 凭据问题），
    而响应上可能长得一样（都是 401）。合成一种失败，事后只能靠猜。
    """
    unavailable = await validate_api_key("any", dev_key="", store=None)
    revoked = await validate_api_key(
        "any", dev_key="", store=_StubKeyStore(None)
    )

    assert unavailable.principal is None
    assert unavailable.details == {"reason": "store_unavailable"}
    assert revoked.principal is None
    assert revoked.details == {"reason": "invalid_or_revoked"}
    assert unavailable.event_type == revoked.event_type == "apikey_auth_failure"


async def test_non_ascii_key_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """非 ASCII 的凭据必须按"不匹配"处理，而不是把 500 打出来。

    WHY 值得钉：``secrets.compare_digest`` 对非 ASCII 的 str 直接抛 TypeError，
    而 Web 侧的 key 来自请求头——一个构造出来的头就能把认证路径变成 500。
    """
    with caplog.at_level(logging.WARNING):
        result = await validate_api_key("密钥", dev_key=_DEV_KEY, store=None)

    assert result.principal is None


async def test_non_ascii_dev_key_is_reported_not_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """配置里的非 ASCII 应急密钥要留下告警。

    WHY：它永远不会匹配，而比对函数不会因此报错——静默的结果是
    "配置明明写了、应急入口却怎么都进不去"，且没有任何线索指向配置。
    """
    with caplog.at_level(logging.WARNING):
        result = await validate_api_key("密钥", dev_key="密钥", store=None)

    assert result.principal is None
    assert "AUTH_API_KEY_DEV" in caplog.text


@pytest.mark.parametrize(
    ("api_key", "dev_key"),
    [(None, ""), ("k", None), (b"k", "")],
)
async def test_type_errors_are_rejected(api_key: object, dev_key: object) -> None:
    """类型错误是调用方的问题，不是一次认证失败。"""
    with pytest.raises(ValueError):
        await validate_api_key(api_key, dev_key=dev_key)  # type: ignore[arg-type]


@pytest.mark.parametrize("api_key", ["", "   "])
async def test_blank_credential_is_a_failure_not_an_error(api_key: str) -> None:
    """空串与全空白按"匹配不上"处理，不抛异常。

    WHY 值得钉：Web 侧的凭据直接来自请求头，把"形态不对"做成异常，等于让一个畸形的头
    把认证路径变成 500——而它的正确结果是 401。
    """
    result = await validate_api_key(api_key, dev_key=_DEV_KEY, store=None)

    assert result.principal is None
    assert result.outcome == "failure"


# ------------------------------------------------------------------ 审计写入


async def test_audit_write_failure_is_swallowed() -> None:
    """审计写坏了不能让认证失败——否则存储故障演变成「全站无法登录」。"""
    await write_auth_audit(
        _BrokenSink(), event_type="apikey_auth_failure", actor_id="unknown", outcome="failure"
    )


async def test_missing_sink_warns_only_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """未装配审计存储只告警一次。

    WHY 只一次：本模块在认证路径上逐个请求被调用，每次打一行 WARNING 等于给扫描器
    一个刷日志的开关——而那正是最需要看清日志的场景。WHY 又要告警：完全静默时
    "全部认证审计被丢掉"在界面上只表现为「面板是空的」，很容易被当成「还没产生事件」。
    """
    monkeypatch.setattr(api_key_auth, "_missing_sink_warned", False)

    with caplog.at_level(logging.WARNING):
        await write_auth_audit(None, event_type="e", actor_id="a", outcome="failure")
        await write_auth_audit(None, event_type="e", actor_id="a", outcome="failure")

    assert caplog.text.count("审计存储未装配") == 1


async def test_entry_marker_is_added_only_when_asked() -> None:
    """入口标记只在调用方要求时写入。

    WHY：Web 侧的审计载荷要逐字保持既有形态（``entry`` 是本次新增的键）；
    而 CLI 侧必须带它——否则它的审计在面板上与一次 HTTP 请求长得完全一样，
    看不出"有人在本机命令行用了这把 Key"。
    """
    result = ApiKeyAuthResult(
        principal=None,
        event_type="apikey_auth_failure",
        actor_id="unknown",
        outcome="failure",
        details={"reason": "invalid_or_revoked"},
    )

    web_sink = _RecordingSink()
    await record_api_key_auth(web_sink, result)
    assert web_sink.calls[0]["details"] == {"reason": "invalid_or_revoked"}

    cli_sink = _RecordingSink()
    await record_api_key_auth(cli_sink, result, entry="cli")
    assert cli_sink.calls[0]["details"] == {
        "reason": "invalid_or_revoked",
        "entry": "cli",
    }
    # action 由本模块统一给出：两个入口写不同的 action，按 action 检索会漏掉一半
    assert cli_sink.calls[0]["action"] == "validate"


async def test_record_does_not_mutate_the_result() -> None:
    """入口标记不能改写结果对象自带的 details。

    WHY：``ApiKeyAuthResult`` 是冻结的，但它的 ``details`` 是可变 dict——
    就地改写会让"结果对象的内容"随调用方而变，同一份结果传两次就得到两种审计。
    """
    result = ApiKeyAuthResult(
        principal=None,
        event_type="apikey_auth_failure",
        actor_id="unknown",
        outcome="failure",
        details={"reason": "store_unavailable"},
    )

    sink = _RecordingSink()
    await record_api_key_auth(sink, result, entry="cli")
    await record_api_key_auth(sink, result)

    assert result.details == {"reason": "store_unavailable"}
    assert "entry" not in sink.calls[1]["details"]
