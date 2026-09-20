"""``interfaces/cli.py`` 的回归测试。

WHY 现在补上这组用例：覆盖率表显示该模块 223 语句、0% 覆盖——不是「无逻辑可测」，
而是从未被测量。它承载三类 Web 侧测不到的分支：终端的事件渲染、stdin 驱动的审批
循环、主循环的退出码语义。缺了它，CI 的覆盖率门槛长期少算一大块生产代码。

WHY 装配用替身而不是真实上下文：真实 ``build_app_context`` 会建库、连模型、编译图，
而本组用例要断言的是「CLI 自己的分支与退出码」。装配路径由 bootstrap 侧覆盖，
替换它才能让失败直接指向 CLI 的行为，而不是基础设施。

WHY 断言落在 stdout 与退出码上：CLI 与用户的全部契约就是这两样东西。

覆盖率说明：本模块 223 语句中仅 ``_build_cli_principal`` 末尾的 ``return None``
未覆盖。它对应「``auth_mode`` 不是两种已知值」的情形，而该字段的类型是
``Literal["disabled", "apikey"]``，合法配置下不可达。为一行不可达的兜底
去伪造非法配置，只会得到一条伪装成测试的断言，因此保留这 1 行缺口。
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from application.errors import ThreadBusyError
from application.events import AgentEvent, AgentEventType
from application.principal import ROLE_PERMISSIONS, Principal
from config import AppConfig
from interfaces import cli
from interfaces.cli import (
    _allowed_decisions,
    _build_cli_principal,
    _format_args,
    _run_turn,
    _validate_api_key_for_cli,
    ask_human,
    main_sync,
    render_event,
    run_cli,
)
from tests.conftest import make_config

# ------------------------------------------------------------------ 输入替身

_EOF = object()
"""放进输入队列表示「此处读 stdin 会立刻 EOF」（管道输入已读完的场景）。"""


def _feed_input(monkeypatch: pytest.MonkeyPatch, *values: Any) -> None:
    """把 ``input()`` 换成按序返回预置值的替身。

    WHY 按序喂值而不是重定向 stdin：CLI 的询问点分散在主循环与审批循环里，
    按序喂值才能精确表达「第几次询问回答什么」，也让「无效输入后重问同一问题」
    这类路径可以逐字断言；队列耗尽即抛错，避免用例悄悄多读一次输入。
    """
    queue = list(values)

    def fake_input(prompt: str = "") -> str:
        if not queue:
            raise AssertionError(f"input() 调用次数超出预置（prompt={prompt!r}）")
        item = queue.pop(0)
        if item is _EOF:
            raise EOFError
        return str(item)

    monkeypatch.setattr("builtins.input", fake_input)


# ------------------------------------------------------------------ 装配替身


class _StubThreads:
    """``ThreadService`` 替身：只提供主循环取用的发号入口。"""

    def new_thread_id(self) -> str:
        return "thread-cli-test"


class _StubModelInfo:
    def __init__(self, name: str) -> None:
        self.name = name


class _StubCatalog:
    """``ModelCatalog`` 替身：只提供别名清单。"""

    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_models(self) -> list[_StubModelInfo]:
        return [_StubModelInfo(name) for name in self._names]


class _StubRuns:
    """``RunService`` 替身：按调用顺序吐出预置事件流，并记录每次调用的入参。

    WHY 记录入参而不只吐事件：``model_name`` 与 ``principal`` 是否正确传到中断
    恢复的那一半，是「一轮运行的前后半程不得换模型 / 换主体」这条约定唯一可
    断言的面。
    """

    def __init__(self, rounds: list[list[AgentEvent]] | None = None) -> None:
        self._remaining = list(rounds or [])
        self.stream_calls: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []

    async def stream(
        self,
        thread_id: str,
        user_input: str,
        *,
        principal: Principal | None = None,
        model_name: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self.stream_calls.append(
            {
                "thread_id": thread_id,
                "user_input": user_input,
                "principal": principal,
                "model_name": model_name,
            }
        )
        return self._next_events()

    async def resume(
        self,
        thread_id: str,
        decision: dict[str, Any],
        *,
        principal: Principal | None = None,
        model_name: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self.resume_calls.append(
            {
                "thread_id": thread_id,
                "decision": decision,
                "principal": principal,
                "model_name": model_name,
            }
        )
        return self._next_events()

    async def _next_events(self) -> AsyncIterator[AgentEvent]:
        if not self._remaining:
            raise AssertionError("事件流被消费的次数超出预置轮数")
        for event in self._remaining.pop(0):
            yield event


class _StubApiKeyStore:
    """``APIKeyRepository`` 替身：返回预置记录，并记录被查询过的密钥。"""

    def __init__(self, record: dict[str, Any] | None = None) -> None:
        self._record = record
        self.validated: list[str] = []

    async def validate(self, api_key: str) -> dict[str, Any] | None:
        self.validated.append(api_key)
        return self._record


class _StubAuditStore:
    """``AuditSink`` 替身：记录每次写入的字段。

    WHY 记录字段而不是只记调用次数：这条路径上真正要守的是"记了什么"
    （事件类型 / actor / 失败原因 / 入口标记），只数次数的话，
    把 event_type 写反也能过。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def log(self, **fields: Any) -> None:
        self.calls.append(fields)


class _StubContext:
    """``build_app_context`` 的替身：只暴露主循环真正取用的几个对象。"""

    def __init__(
        self,
        threads: Any,
        runs: Any,
        catalog: Any,
        api_key_store: Any = None,
        audit_store: Any = None,
    ) -> None:
        self.threads = threads
        self.runs = runs
        self.catalog = catalog
        self.api_key_store = api_key_store
        # WHY 必须有这一项：CLI 的认证审计与 Web 落进同一个存储（由 bootstrap 装配）。
        # 替身少这一个字段，主循环会在取值时抛 AttributeError——那不是被测代码的问题，
        # 而是替身没跟上 AppContext 的形状。
        self.audit_store = audit_store

    async def __aenter__(self) -> _StubContext:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _patch_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_names: list[str] | None = None,
    rounds: list[list[AgentEvent]] | None = None,
    api_key_store: Any = None,
    audit_store: Any = None,
) -> _StubRuns:
    """替换 ``build_app_context``，并返回其中的假 ``RunService`` 供断言。"""
    runs = _StubRuns(rounds)
    context = _StubContext(
        threads=_StubThreads(),
        runs=runs,
        catalog=_StubCatalog(model_names or ["stub-model"]),
        api_key_store=api_key_store,
        audit_store=audit_store,
    )
    monkeypatch.setattr(cli, "build_app_context", lambda config: context)
    return runs


def _event(kind: AgentEventType, **payload: Any) -> AgentEvent:
    return AgentEvent(kind, dict(payload))


def _principal() -> Principal:
    return Principal(user_id="u1", role="member", auth_method="apikey")


# ================================================================== 事件渲染


def test_render_token_does_not_break_line(capsys):
    render_event(_event(AgentEventType.TOKEN, text="你"))
    render_event(_event(AgentEventType.TOKEN, text="好"))

    # WHY 断言无换行：流式输出若每片一行，终端里会把一句话拆成多行
    assert capsys.readouterr().out == "你好"


def test_render_tool_call_flattens_args(capsys):
    render_event(
        _event(
            AgentEventType.TOOL_CALL,
            name="read_file",
            args={"path": "a.txt", "offset": 1},
        )
    )

    out = capsys.readouterr().out
    assert "[调用] read_file" in out
    assert "path=a.txt" in out


def test_render_tool_result_marks_truncation(capsys):
    render_event(
        _event(AgentEventType.TOOL_RESULT, name="execute", status="success", truncated=True)
    )
    render_event(_event(AgentEventType.TOOL_RESULT, name="execute", status="success"))

    out = capsys.readouterr().out
    assert "[结果] execute success (已截断)" in out
    assert "[结果] execute success\n" in out


def test_render_todos_uses_marker_per_status(capsys):
    render_event(
        _event(
            AgentEventType.TODOS,
            items=[
                {"content": "做完了", "status": "completed"},
                {"content": "在做", "status": "in_progress"},
                {"content": "没开始", "status": "pending"},
                {"content": "状态缺失"},
            ],
        )
    )

    out = capsys.readouterr().out
    assert "[x] 做完了" in out
    assert "[>] 在做" in out
    # 状态由模型自由填写，出现新值时必须退化为「未开始」而不是让整轮对话崩掉
    assert out.count("[ ]") == 2


def test_render_step_is_silent(capsys):
    render_event(_event(AgentEventType.STEP, node="agent"))

    # 节点级进度只是给 Web 前端做细粒度进度的，终端渲染它会把输出刷乱
    assert capsys.readouterr().out == ""


def test_render_error_prints_message(capsys):
    render_event(_event(AgentEventType.ERROR, message="炸了"))

    assert "[错误] 炸了" in capsys.readouterr().out


def test_render_usage_prints_all_three_totals(capsys):
    render_event(
        _event(AgentEventType.USAGE, prompt_tokens=10, completion_tokens=5, total_tokens=15)
    )

    assert "输入 10 / 输出 5 / 合计 15 tokens" in capsys.readouterr().out


def test_render_done_breaks_line(capsys):
    render_event(_event(AgentEventType.DONE))

    assert capsys.readouterr().out == "\n"


# ================================================================== 参数摘要


def test_format_args_prefers_raw_text():
    # ``__raw__`` 是参数无法解析成 JSON 时的原文兜底，此时再逐键展开只会得到空摘要
    assert _format_args({"__raw__": "not-json-at-all"}) == "not-json-at-all"


def test_format_args_caps_keys_and_value_length():
    summary = _format_args({f"k{index}": "x" * 100 for index in range(6)})

    parts = summary.split(", ")
    # 只展开前 4 个键、每个值截到 40 字符，否则一次工具调用就能刷满整屏
    assert len(parts) == 4
    assert all(part.endswith("x" * 40) for part in parts)


def test_format_args_falls_back_to_str():
    assert _format_args(["a", "b"]) == "['a', 'b']"


# ================================================================== 审批选项


def test_allowed_decisions_reads_matching_config():
    configs = [{"action_name": "execute", "allowed_decisions": ["approve", "reject", "edit"]}]

    assert _allowed_decisions("execute", configs) == ["approve", "reject", "edit"]


def test_allowed_decisions_defaults_for_unknown_action():
    # 没有匹配条目时退化为「批准 / 拒绝」：宁可少给选项，也不能默认放开 edit
    assert _allowed_decisions("unknown", []) == ["approve", "reject"]


def test_allowed_decisions_defaults_when_key_missing():
    assert _allowed_decisions("execute", [{"action_name": "execute"}]) == ["approve", "reject"]


# ================================================================== 人工审批


def test_ask_human_accepts_short_alias(monkeypatch, capsys):
    _feed_input(monkeypatch, "a")

    result = ask_human(
        {
            "action_requests": [
                {"name": "execute", "description": "跑命令", "args": {"cmd": "ls"}}
            ],
            "review_configs": [],
        }
    )

    assert result == {"decisions": [{"type": "approve"}]}
    out = capsys.readouterr().out
    assert "需要审批" in out
    assert "工具：execute" in out
    # 没有 review_configs 时只放开批准与拒绝：选项行必须与真实可选项一致
    assert "可选：a=approve, r=reject" in out


def test_ask_human_accepts_full_decision_word(monkeypatch):
    _feed_input(monkeypatch, "approve")

    result = ask_human({"action_requests": [{"name": "execute", "args": {}}]})

    assert result == {"decisions": [{"type": "approve"}]}


def test_ask_human_retries_on_invalid_then_rejects_with_message(monkeypatch, capsys):
    _feed_input(monkeypatch, "z", "r", "因为危险")

    result = ask_human({"action_requests": [{"name": "execute", "args": {}}]})

    assert result == {"decisions": [{"type": "reject", "message": "因为危险"}]}
    assert "无效输入" in capsys.readouterr().out


def test_ask_human_refuses_decision_outside_allowlist(monkeypatch, capsys):
    """WHY 覆盖这条：``allowed_decisions`` 是审批链的安全边界——配置只允许
    批准 / 拒绝时，用户敲 ``e`` 不能改参数后放行。"""
    _feed_input(monkeypatch, "e", "a")

    result = ask_human(
        {
            "action_requests": [{"name": "execute", "args": {}}],
            "review_configs": [
                {"action_name": "execute", "allowed_decisions": ["approve", "reject"]}
            ],
        }
    )

    assert result == {"decisions": [{"type": "approve"}]}
    out = capsys.readouterr().out
    assert "无效输入" in out
    assert "e=edit" not in out


def test_ask_human_edit_retries_on_bad_json(monkeypatch, capsys):
    _feed_input(monkeypatch, "e", "{not json", "e", '{"cmd": "ls"}')

    result = ask_human(
        {
            "action_requests": [{"name": "execute", "args": {}}],
            "review_configs": [
                {"action_name": "execute", "allowed_decisions": ["approve", "edit"]}
            ],
        }
    )

    assert result == {
        "decisions": [
            {"type": "edit", "edited_action": {"name": "execute", "args": {"cmd": "ls"}}}
        ]
    }
    # JSON 写坏时必须重问同一个问题，而不是静默丢弃这次修改
    assert "JSON 解析失败" in capsys.readouterr().out


def test_ask_human_handles_multiple_requests(monkeypatch):
    _feed_input(monkeypatch, "a", "s", "改用另一个路径")

    result = ask_human(
        {
            "action_requests": [
                {"name": "execute", "args": {}},
                {"name": "write_file", "args": {}},
            ],
            # 第二个动作单独放开 respond，用来验证「每个动作各取自己的可选集」
            "review_configs": [
                {"action_name": "write_file", "allowed_decisions": ["approve", "respond"]}
            ],
        }
    )

    assert result == {
        "decisions": [
            {"type": "approve"},
            {"type": "respond", "message": "改用另一个路径"},
        ]
    }


def test_ask_human_returns_empty_decisions_without_requests():
    assert ask_human({}) == {"decisions": []}


# ================================================================== API Key 校验


async def test_validate_api_key_accepts_dev_key(tmp_path: Path):
    config = make_config(tmp_path, auth_api_key_dev="dev-secret")

    principal = await _validate_api_key_for_cli(config, "dev-secret")

    assert principal == Principal(
        user_id="apikey:dev",
        display_name="dev",
        role="admin",
        # WHY 断言 scopes：同一个应急密钥换来的主体，两个入口必须长得一样。
        # 此前 CLI 侧不带 scopes、Web 侧带（Web 把 admin 的权限集给了它），
        # 而 scopes 会经 /auth/me 回给浏览器，也是 has_scope 唯一的输入——
        # 一旦它参与权限判断，CLI 侧会静默少权限。
        scopes=frozenset(ROLE_PERMISSIONS["admin"]),
        auth_method="apikey",
    )


async def test_validate_api_key_checks_dev_key_before_store(tmp_path: Path):
    config = make_config(tmp_path, auth_api_key_dev="dev-secret")
    store = _StubApiKeyStore({"key_id": "k1", "key_prefix": "abcd", "role": "member"})

    principal = await _validate_api_key_for_cli(config, "dev-secret", api_key_store=store)

    assert principal.user_id == "apikey:dev"
    # 命中 dev key 后不该再查库：多一次查询没有意义，也给了库侧记录夺走应急入口的机会
    assert store.validated == []


async def test_validate_api_key_uses_store_record(tmp_path: Path):
    config = make_config(tmp_path)
    store = _StubApiKeyStore(
        {
            "key_id": "k1",
            "key_prefix": "abcd",
            "role": "member",
            "scopes": "usage:read audit:read",
        }
    )

    principal = await _validate_api_key_for_cli(config, "key-1", api_key_store=store)

    assert principal.user_id == "apikey:k1"
    assert principal.display_name == "API Key abcd..."
    assert principal.role == "member"
    assert principal.scopes == frozenset({"usage:read", "audit:read"})
    assert store.validated == ["key-1"]


async def test_validate_api_key_rejects_unknown_key(tmp_path: Path):
    config = make_config(tmp_path)

    with pytest.raises(ValueError, match="HARNESS_API_KEY 无效"):
        await _validate_api_key_for_cli(config, "nope", api_key_store=_StubApiKeyStore())


# ================================================================== 认证审计


async def test_validate_api_key_for_cli_writes_success_audit(tmp_path: Path):
    """CLI 认证成功要落审计，并标出入口。

    WHY 单独立一条：这条审计此前**根本不存在**。两个入口的认证语义没有收敛时，
    Web 侧成功/失败各落一条，CLI 侧一条都不落——而两边都"能登录"，
    功能测试永远发现不了这种差异。
    """
    config = make_config(tmp_path, auth_api_key_dev="dev-secret")
    audit = _StubAuditStore()

    await _validate_api_key_for_cli(config, "dev-secret", audit_store=audit)

    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["event_type"] == "apikey_auth_success"
    assert call["actor_id"] == "apikey:dev"
    assert call["outcome"] == "success"
    assert call["action"] == "validate"
    assert call["details"] == {"source": "env_dev_key", "entry": "cli"}
    # CLI 没有来源地址与客户端标识。传空串会让审计面板上出现一个看起来像异常的空白值，
    # 而"这个字段不存在"与"取到了空值"是两回事。
    assert call["ip"] is None
    assert call["user_agent"] is None


async def test_validate_api_key_for_cli_writes_failure_audit_before_raising(tmp_path: Path):
    """失败也要落审计，且在抛异常之前落。

    WHY 时序也要测：失败路径上只有这一层拿得到结果对象——上层只看到一个异常，
    写不出"为什么失败"，而"存储没装配"与"凭据无效"的处置方式完全不同。
    """
    config = make_config(tmp_path)
    audit = _StubAuditStore()

    with pytest.raises(ValueError, match="HARNESS_API_KEY 无效"):
        await _validate_api_key_for_cli(
            config, "nope", api_key_store=_StubApiKeyStore(), audit_store=audit
        )

    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["event_type"] == "apikey_auth_failure"
    assert call["actor_id"] == "unknown"
    assert call["outcome"] == "failure"
    assert call["details"] == {"reason": "invalid_or_revoked", "entry": "cli"}


async def test_audit_write_failure_does_not_break_cli_auth(tmp_path: Path):
    """审计写坏了也不能让认证失败——否则存储故障直接演变成「谁都登录不了」。"""
    config = make_config(tmp_path, auth_api_key_dev="dev-secret")

    class _BrokenAuditStore:
        async def log(self, **fields: Any) -> None:
            raise RuntimeError("审计库炸了")

    principal = await _validate_api_key_for_cli(
        config, "dev-secret", audit_store=_BrokenAuditStore()
    )

    assert principal.user_id == "apikey:dev"


# ================================================================== 主体构造


def _cli_auth_config(tmp_path: Path, mode: str, **overrides: Any) -> AppConfig:
    """构造用于 apikey 模式的配置。"""
    params: dict[str, Any] = {"auth_mode": mode}
    params.update(overrides)
    return make_config(tmp_path, **params)


async def test_build_principal_disabled_returns_none(tmp_path: Path):
    assert await _build_cli_principal(make_config(tmp_path)) is None


async def test_build_principal_apikey_requires_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HARNESS_API_KEY", raising=False)
    config = _cli_auth_config(tmp_path, "apikey")

    with pytest.raises(ValueError, match="HARNESS_API_KEY"):
        await _build_cli_principal(config)


async def test_build_principal_apikey_uses_env_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "dev-secret")
    config = _cli_auth_config(tmp_path, "apikey", auth_api_key_dev="dev-secret")

    principal = await _build_cli_principal(config)

    assert principal is not None
    assert principal.user_id == "apikey:dev"


async def test_build_principal_apikey_reads_key_from_env_file(tmp_path: Path, monkeypatch):
    """写在 ``.env`` 里的密钥必须生效——它是 .env.example 指的配置位置。

    WHY 单独测：``.env`` 的值只进 ``AppConfig``、不进 ``os.environ``，所以「CLI 直接读
    环境变量」的实现会让这条路径永远失败，而它恰是容器内与「照模板配好」两条真实场景
    的主路径；症状是提示「没配密钥」而用户刚刚才配过。
    """
    monkeypatch.delenv("HARNESS_API_KEY", raising=False)
    config = _cli_auth_config(
        tmp_path,
        "apikey",
        harness_api_key="dev-secret",
        auth_api_key_dev="dev-secret",
    )

    principal = await _build_cli_principal(config)

    assert principal is not None
    assert principal.user_id == "apikey:dev"


async def test_build_principal_apikey_rejects_invalid_env_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "wrong")
    config = _cli_auth_config(tmp_path, "apikey", auth_api_key_dev="dev-secret")

    with pytest.raises(ValueError, match="无效"):
        await _build_cli_principal(config)


async def test_run_turn_renders_events_without_any_interrupt(capsys):
    runs = _StubRuns([[_event(AgentEventType.TOKEN, text="hi"), _event(AgentEventType.DONE)]])

    await _run_turn(runs, "t1", "你好", model_name="m")

    assert "hi" in capsys.readouterr().out
    assert runs.stream_calls == [
        {
            "thread_id": "t1",
            "user_input": "你好",
            "principal": None,
            "model_name": "m",
        }
    ]
    assert runs.resume_calls == []


async def test_run_turn_handles_interrupt_then_resumes(capsys, monkeypatch):
    principal = _principal()
    runs = _StubRuns(
        [
            [
                _event(AgentEventType.INTERRUPT, action_requests=[{"name": "execute"}]),
                _event(AgentEventType.DONE),
            ],
            [_event(AgentEventType.TOKEN, text="恢复后"), _event(AgentEventType.DONE)],
        ]
    )
    monkeypatch.setattr(cli, "ask_human", lambda payload: {"decisions": [{"type": "approve"}]})

    await _run_turn(runs, "t1", "跑一下", principal=principal, model_name="m")

    # 中断本身不该被渲染成普通事件：它要交给审批循环，打印出来只会让终端多一段噪音
    assert capsys.readouterr().out.strip() == "恢复后"
    assert len(runs.resume_calls) == 1
    resume_call = runs.resume_calls[0]
    assert resume_call["thread_id"] == "t1"
    assert resume_call["decision"] == {"decisions": [{"type": "approve"}]}
    # 恢复必须带同一个 model_name：前后半程换模型会多建一个实例，成本与行为都不可预期
    assert resume_call["model_name"] == "m"
    assert resume_call["principal"] == principal


async def test_run_turn_handles_two_interrupts_in_one_turn(monkeypatch):
    """WHY 覆盖多次中断：一次工具调用可以逐个动作触发审批，只处理第一个的话
    第二轮审批会被静默丢弃，运行永远停在那里。"""
    runs = _StubRuns(
        [
            [_event(AgentEventType.INTERRUPT, action_requests=[{"name": "execute"}])],
            [_event(AgentEventType.INTERRUPT, action_requests=[{"name": "write_file"}])],
            [_event(AgentEventType.DONE)],
        ]
    )
    monkeypatch.setattr(cli, "ask_human", lambda payload: {"decisions": [{"type": "approve"}]})

    await _run_turn(runs, "t1", "连着两次")

    assert len(runs.resume_calls) == 2
    assert len(runs.stream_calls) == 1


# ================================================================== 主循环


async def test_run_cli_rejects_none_config():
    with pytest.raises(ValueError, match="config 不能为 None"):
        await run_cli(None)  # type: ignore[arg-type]


async def test_run_cli_returns_2_for_unknown_model(tmp_path: Path, monkeypatch, capsys):
    config = make_config(tmp_path)
    _feed_input(monkeypatch, "exit")
    _patch_context(monkeypatch, model_names=["deepseek-flash"])

    code = await run_cli(config, model_name="nope")

    assert code == 2
    assert "未知模型" in capsys.readouterr().out


async def test_run_cli_uses_default_model_when_not_given(tmp_path: Path, monkeypatch, capsys):
    config = make_config(tmp_path)
    _feed_input(monkeypatch, "exit")
    _patch_context(monkeypatch, model_names=[config.default_model])

    assert await run_cli(config) == 0

    out = capsys.readouterr().out
    assert f"当前模型：{config.default_model}" in out
    assert "会话 ID：thread-cli-test" in out


async def test_run_cli_runs_one_turn_and_exits_on_quit_alias(tmp_path: Path, monkeypatch, capsys):
    config = make_config(tmp_path)
    _feed_input(monkeypatch, "你好", ":q")
    runs = _patch_context(
        monkeypatch,
        model_names=["stub-model"],
        rounds=[[_event(AgentEventType.TOKEN, text="回答"), _event(AgentEventType.DONE)]],
    )

    code = await run_cli(config, model_name="stub-model")

    assert code == 0
    assert "回答" in capsys.readouterr().out
    assert len(runs.stream_calls) == 1
    # auth_mode=disabled 时主体为 None，走匿名兼容路径
    assert runs.stream_calls[0]["principal"] is None


async def test_run_cli_skips_blank_input(tmp_path: Path, monkeypatch):
    config = make_config(tmp_path)
    _feed_input(monkeypatch, "", "   ", "exit")
    runs = _patch_context(monkeypatch, model_names=["stub-model"])

    assert await run_cli(config, model_name="stub-model") == 0

    # 空行是最常见的误触（多按一次回车），不该被当成一次提问发起运行
    assert runs.stream_calls == []


async def test_run_cli_exits_cleanly_on_eof(tmp_path: Path, monkeypatch):
    """WHY 覆盖这条：``echo hi | python main.py cli`` 这类管道用法读完 stdin 就抛
    EOFError。当成异常处理会让正常的脚本化调用返回非零退出码。"""
    config = make_config(tmp_path)
    _feed_input(monkeypatch, _EOF)
    _patch_context(monkeypatch, model_names=["stub-model"])

    assert await run_cli(config, model_name="stub-model") == 0


async def test_run_cli_exits_zero_on_keyboard_interrupt(tmp_path: Path, monkeypatch, capsys):
    config = make_config(tmp_path)
    _feed_input(monkeypatch, "你好")
    runs = _patch_context(monkeypatch, model_names=["stub-model"])

    async def interrupt(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(runs, "stream", interrupt)

    # Ctrl+C 是用户的正常结束方式，退出码 0 才能让 shell 脚本里的 `agent cli` 不被当成失败
    assert await run_cli(config, model_name="stub-model") == 0
    assert "已中断" in capsys.readouterr().out


async def test_run_cli_reports_busy_thread_as_recoverable(tmp_path: Path, monkeypatch, capsys):
    config = make_config(tmp_path)
    _feed_input(monkeypatch, "你好")
    runs = _patch_context(monkeypatch, model_names=["stub-model"])

    async def busy(*args: Any, **kwargs: Any) -> Any:
        raise ThreadBusyError("t-busy")

    monkeypatch.setattr(runs, "stream", busy)

    code = await run_cli(config, model_name="stub-model")

    assert code == 1
    assert "会话 t-busy 正在运行中" in capsys.readouterr().out


async def test_run_cli_logs_unexpected_failure(tmp_path: Path, monkeypatch, caplog):
    config = make_config(tmp_path)
    _feed_input(monkeypatch, "你好")
    runs = _patch_context(monkeypatch, model_names=["stub-model"])

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("炸了")

    monkeypatch.setattr(runs, "stream", boom)

    with caplog.at_level(logging.ERROR):
        code = await run_cli(config, model_name="stub-model")

    assert code == 1
    # 未预期异常必须留下堆栈：只打印一句话会让线上排障无从下手
    assert "CLI 运行失败" in caplog.text


async def test_run_cli_uses_authenticated_principal(tmp_path: Path, monkeypatch):
    # WHY 顺序不能反：凭据与其余配置同一个入口（``AppConfig.harness_api_key``），
    # 而配置在构造期读环境变量——先建配置再设变量，得到的是「变量明明设了却没生效」，
    # 正是这条用例要防的形态（真实启动顺序也是先有环境、再建配置）。
    monkeypatch.setenv("HARNESS_API_KEY", "dev-secret")
    config = _cli_auth_config(tmp_path, "apikey", auth_api_key_dev="dev-secret")
    _feed_input(monkeypatch, "你好", "exit")
    runs = _patch_context(
        monkeypatch,
        model_names=["stub-model"],
        rounds=[[_event(AgentEventType.DONE)]],
    )

    assert await run_cli(config, model_name="stub-model") == 0

    principal = runs.stream_calls[0]["principal"]
    assert principal is not None
    assert principal.user_id == "apikey:dev"


async def test_run_cli_returns_2_when_api_key_missing(tmp_path: Path, monkeypatch, capsys):
    """``auth_mode=apikey`` 却没给密钥：当场说清并返回 2，不抛堆栈。

    WHY 断言这条：此前这个 ValueError 一路抛到 ``main()``，用户看到的是三层堆栈，
    真正的一句话提示被夹在中间；退出码 2（参数错误）也让脚本能区分「配置没给」与
    「跑起来之后失败」（后者是 1）。
    """
    monkeypatch.delenv("HARNESS_API_KEY", raising=False)
    config = _cli_auth_config(tmp_path, "apikey")
    runs = _patch_context(monkeypatch, model_names=["stub-model"])

    code = await run_cli(config, model_name="stub-model")
    out = capsys.readouterr().out

    assert code == 2
    assert "HARNESS_API_KEY" in out
    assert "AUTH_API_KEY_DEV" in out
    # 提示与退出码都对了，就说明这一步没有半途发起过任何运行
    assert runs.stream_calls == []


async def test_run_cli_returns_2_when_api_key_invalid(tmp_path: Path, monkeypatch, capsys):
    """密钥填错同样是配置问题，同样按 2 返回并给出提示。"""
    monkeypatch.setenv("HARNESS_API_KEY", "wrong")
    config = _cli_auth_config(tmp_path, "apikey", auth_api_key_dev="dev-secret")
    _patch_context(monkeypatch, model_names=["stub-model"])

    assert await run_cli(config, model_name="stub-model") == 2
    assert "无效" in capsys.readouterr().out


# ================================================================== 同步入口


def test_main_sync_delegates_to_asyncio_run(tmp_path: Path, monkeypatch):
    recorded: dict[str, Any] = {}

    async def fake_run_cli(config: AppConfig, *, model_name: str | None = None) -> int:
        recorded["config"] = config
        recorded["model_name"] = model_name
        return 7

    monkeypatch.setattr(cli, "run_cli", fake_run_cli)
    config = make_config(tmp_path)

    assert main_sync(config, model_name="m") == 7
    assert recorded == {"config": config, "model_name": "m"}
