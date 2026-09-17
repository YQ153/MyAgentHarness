"""``interfaces/cli.py`` 的回归测试。

WHY 现在补上这组用例：覆盖率表显示该模块 223 语句、0% 覆盖——不是「无逻辑可测」，
而是从未被测量。它承载三类 Web 侧测不到的分支：终端的事件渲染、stdin 驱动的审批
循环、主循环的退出码语义。缺了它，CI 的覆盖率门槛长期少算一大块生产代码。

WHY 装配用替身而不是真实上下文：真实 ``build_app_context`` 会建库、连模型、编译图，
而本组用例要断言的是「CLI 自己的分支与退出码」。装配路径由 bootstrap 侧覆盖，
替换它才能让失败直接指向 CLI 的行为，而不是基础设施。

WHY 断言落在 stdout 与退出码上：CLI 与用户的全部契约就是这两样东西。

覆盖率说明：本模块 223 语句中仅 ``_build_cli_principal`` 末尾的 ``return None``
未覆盖。它对应「``auth_mode`` 不是三种已知值」的情形，而该字段的类型是
``Literal["disabled", "apikey", "oidc"]``，合法配置下不可达。为一行不可达的兜底
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
from application.principal import Principal
from config import AppConfig
from interfaces import cli
from interfaces.cli import (
    _allowed_decisions,
    _authenticate_oidc_device_flow,
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


class _StubContext:
    """``build_app_context`` 的替身：只暴露主循环真正取用的四个对象。"""

    def __init__(
        self,
        threads: Any,
        runs: Any,
        catalog: Any,
        api_key_store: Any = None,
    ) -> None:
        self.threads = threads
        self.runs = runs
        self.catalog = catalog
        self.api_key_store = api_key_store

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
) -> _StubRuns:
    """替换 ``build_app_context``，并返回其中的假 ``RunService`` 供断言。"""
    runs = _StubRuns(rounds)
    context = _StubContext(
        threads=_StubThreads(),
        runs=runs,
        catalog=_StubCatalog(model_names or ["stub-model"]),
        api_key_store=api_key_store,
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


# ================================================================== 主体构造


def _cli_auth_config(tmp_path: Path, mode: str, **overrides: Any) -> AppConfig:
    """构造可用于非 disabled 认证的配置。

    WHY 必须补齐这些字段：``AppConfig`` 有前置校验——非 disabled 模式要求
    ``auth_session_secret`` 至少 32 字节，oidc 模式还要求 issuer / client_id /
    client_secret / redirect_uri 非空。缺一个，构造阶段就会失败，用例根本走不到 CLI。
    """
    params: dict[str, Any] = {
        "auth_mode": mode,
        "auth_session_secret": "s" * 32,
    }
    if mode == "oidc":
        params.update(
            {
                "oidc_issuer": "http://issuer.test",
                "oidc_client_id": "cid",
                "oidc_client_secret": "csecret",
                "oidc_redirect_uri": "http://127.0.0.1:8000/auth/callback",
                "oidc_device_flow_base_url": "http://auth.test",
            }
        )
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


async def test_build_principal_apikey_rejects_invalid_env_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "wrong")
    config = _cli_auth_config(tmp_path, "apikey", auth_api_key_dev="dev-secret")

    with pytest.raises(ValueError, match="无效"):
        await _build_cli_principal(config)


async def test_build_principal_oidc_prefers_env_key(tmp_path: Path, monkeypatch):
    """WHY 覆盖这条：已有 Key 时不该再走一次 Device Flow——那会打开浏览器、等用户
    点批准，把一次「直接可用」的启动变成交互。"""
    monkeypatch.setenv("HARNESS_API_KEY", "dev-secret")
    config = _cli_auth_config(tmp_path, "oidc", auth_api_key_dev="dev-secret")

    principal = await _build_cli_principal(config)

    assert principal is not None
    assert principal.user_id == "apikey:dev"


async def test_build_principal_oidc_runs_device_flow_and_caches_key(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("HARNESS_API_KEY", raising=False)
    config = _cli_auth_config(tmp_path, "oidc", auth_api_key_dev="key-from-flow")

    async def fake_device_flow(inner_config: AppConfig) -> str:
        return "key-from-flow"

    monkeypatch.setattr(cli, "_authenticate_oidc_device_flow", fake_device_flow)

    principal = await _build_cli_principal(config)

    assert principal is not None
    assert principal.user_id == "apikey:dev"
    # 换到的 Key 必须回填环境变量，同进程内后续取用（如子流程）不必再走一遍流程
    assert os.environ["HARNESS_API_KEY"] == "key-from-flow"


# ================================================================== Device Flow


class _StubResponse:
    """httpx 响应的最小替身：只提供流程真正读取的三个口。"""

    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: Any = None,
        error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._error = error

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("响应体不是 JSON")
        return self._payload

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error


class _StubHttpClient:
    """``httpx.AsyncClient`` 替身：按 URL 返回预置响应，并记录全部请求。

    轮询队列里的元素可以是 ``_StubResponse``（正常响应），也可以是异常实例
    （模拟单次轮询的网络抖动），后者用于覆盖「抖动后继续轮询」这条分支。
    """

    def __init__(self, *, authorize: Any, token: Any) -> None:
        self._authorize = authorize
        self._token = token if isinstance(token, list) else [token]
        self.posts: list[tuple[str, Any]] = []

    async def __aenter__(self) -> _StubHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, json: Any = None) -> _StubResponse:
        self.posts.append((url, json))
        if url.endswith("/auth/device/authorize"):
            if isinstance(self._authorize, Exception):
                raise self._authorize
            return self._authorize
        if not self._token:
            raise AssertionError("轮询次数超出预置响应")
        item = self._token.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _StubHttpx:
    """``httpx`` 模块替身：只换掉 ``AsyncClient``，异常类型透传真实实现。

    WHY 不直接 patch ``httpx.AsyncClient``：那是进程级全局对象，替身一旦泄漏到
    其它用例，真实网络调用会静默变成 stub 返回值；换成模块级替身可把影响面收在
    ``interfaces.cli`` 内。
    """

    HTTPError = httpx.HTTPError

    def __init__(self, client: _StubHttpClient) -> None:
        self._client = client

    def AsyncClient(self, **kwargs: Any) -> _StubHttpClient:
        return self._client


class _StubBrowser:
    """``webbrowser`` 替身：记录被请求打开的地址，避免测试真的弹窗口。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.opened: list[str] = []
        self._fail = fail

    def open(self, url: str) -> bool:
        self.opened.append(url)
        if self._fail:
            raise OSError("没有可用的浏览器")
        return True


def _patch_device_flow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    authorize: Any,
    token: Any,
    browser: _StubBrowser | None = None,
) -> tuple[_StubHttpClient, _StubBrowser]:
    client = _StubHttpClient(authorize=authorize, token=token)
    stub_browser = browser or _StubBrowser()
    monkeypatch.setattr(cli, "httpx", _StubHttpx(client))
    monkeypatch.setattr(cli, "webbrowser", stub_browser)
    return client, stub_browser


def _authorize_ok(**overrides: Any) -> _StubResponse:
    payload: dict[str, Any] = {"user_code": "ABCD-EFGH", "device_code": "dc-1", "interval": 0}
    payload.update(overrides)
    return _StubResponse(payload=payload)


async def test_device_flow_returns_access_token(tmp_path: Path, monkeypatch, capsys):
    config = _cli_auth_config(tmp_path, "oidc")
    client, browser = _patch_device_flow(
        monkeypatch,
        authorize=_authorize_ok(),
        token=_StubResponse(payload={"access_token": "key-from-flow"}),
    )

    token = await _authenticate_oidc_device_flow(config)

    assert token == "key-from-flow"
    assert client.posts == [
        ("http://auth.test/auth/device/authorize", None),
        ("http://auth.test/auth/device/token", {"device_code": "dc-1"}),
    ]
    out = capsys.readouterr().out
    assert "ABCD-EFGH" in out
    assert "认证成功" in out
    # 提示用户访问与自动打开浏览器是两件事：提示必须始终打印，打开可以失败
    assert browser.opened == ["http://auth.test/auth/device/activate"]


async def test_device_flow_uses_verification_uri_complete_when_given(
    tmp_path: Path, monkeypatch
):
    config = _cli_auth_config(tmp_path, "oidc")
    _, browser = _patch_device_flow(
        monkeypatch,
        authorize=_authorize_ok(verification_uri_complete="http://auth.test/activate?code=ABCD"),
        token=_StubResponse(payload={"access_token": "k"}),
    )

    await _authenticate_oidc_device_flow(config)

    assert browser.opened == ["http://auth.test/activate?code=ABCD"]


async def test_device_flow_survives_pending_then_succeeds(tmp_path: Path, monkeypatch):
    """WHY 覆盖这条：``authorization_pending`` 是「用户还没点批准」的正常状态。
    把它当失败，用户稍慢一步就会看到 CLI 报错退出。"""
    config = _cli_auth_config(tmp_path, "oidc")
    _patch_device_flow(
        monkeypatch,
        authorize=_authorize_ok(),
        token=[
            _StubResponse(status_code=400, payload={"detail": "authorization_pending"}),
            _StubResponse(status_code=400, payload={"detail": "authorization_pending"}),
            _StubResponse(payload={"access_token": "key-after-pending"}),
        ],
    )

    assert await _authenticate_oidc_device_flow(config) == "key-after-pending"


async def test_device_flow_keeps_polling_after_transport_error(tmp_path: Path, monkeypatch):
    """WHY 覆盖这条：单次轮询的网络抖动不该终止整个流程，否则用户要重新走一遍
    「打开浏览器 → 输入授权码 → 点批准」。"""
    config = _cli_auth_config(tmp_path, "oidc")
    _patch_device_flow(
        monkeypatch,
        authorize=_authorize_ok(),
        token=[
            httpx.HTTPError("连接被重置"),
            _StubResponse(payload={"access_token": "key-after-retry"}),
        ],
    )

    assert await _authenticate_oidc_device_flow(config) == "key-after-retry"


async def test_device_flow_treats_unparseable_400_as_terminal_failure(
    tmp_path: Path, monkeypatch
):
    """WHY 覆盖这条：只有 ``authorization_pending`` 才继续轮询。同样是 400 但读不出
    detail（例如中间的网关返回了 HTML 错误页）属**未知状态**，必须按失败处理——
    把未知状态一律当成「用户还没点批准」会让 CLI 空转到超时，而用户拿不到任何原因。"""
    config = _cli_auth_config(tmp_path, "oidc")
    _patch_device_flow(
        monkeypatch,
        authorize=_authorize_ok(),
        token=_StubResponse(status_code=400),
    )

    with pytest.raises(ValueError, match="Device flow 失败"):
        await _authenticate_oidc_device_flow(config)


async def test_device_flow_raises_when_authorize_unreachable(tmp_path: Path, monkeypatch):
    config = _cli_auth_config(tmp_path, "oidc")
    _patch_device_flow(
        monkeypatch,
        authorize=httpx.HTTPError("无法连接"),
        token=_StubResponse(payload={}),
    )

    with pytest.raises(ValueError, match="无法连接认证服务"):
        await _authenticate_oidc_device_flow(config)


async def test_device_flow_raises_when_authorize_status_is_error(tmp_path: Path, monkeypatch):
    config = _cli_auth_config(tmp_path, "oidc")
    _patch_device_flow(
        monkeypatch,
        authorize=_StubResponse(error=httpx.HTTPError("500 Server Error")),
        token=_StubResponse(payload={}),
    )

    with pytest.raises(ValueError, match="无法连接认证服务"):
        await _authenticate_oidc_device_flow(config)


async def test_device_flow_raises_on_terminal_rejection(tmp_path: Path, monkeypatch):
    config = _cli_auth_config(tmp_path, "oidc")
    _patch_device_flow(
        monkeypatch,
        authorize=_authorize_ok(),
        token=_StubResponse(status_code=400, payload={"detail": "access_denied"}),
    )

    # 用户明确拒绝属于终态，继续轮询只会让 CLI 空转到超时
    with pytest.raises(ValueError, match="access_denied"):
        await _authenticate_oidc_device_flow(config)


async def test_device_flow_raises_on_empty_access_token(tmp_path: Path, monkeypatch):
    config = _cli_auth_config(tmp_path, "oidc")
    _patch_device_flow(
        monkeypatch,
        authorize=_authorize_ok(),
        token=_StubResponse(payload={"access_token": ""}),
    )

    with pytest.raises(ValueError, match="access_token 为空"):
        await _authenticate_oidc_device_flow(config)


async def test_device_flow_raises_on_timeout_without_polling(tmp_path: Path, monkeypatch):
    config = _cli_auth_config(tmp_path, "oidc")
    client, _ = _patch_device_flow(
        monkeypatch,
        authorize=_authorize_ok(expires_in=0),
        token=_StubResponse(payload={"access_token": "never-used"}),
    )

    with pytest.raises(ValueError, match="超时"):
        await _authenticate_oidc_device_flow(config)

    # 已过期时连一次轮询都不该发出：白等的每一秒都是用户的时间
    assert len(client.posts) == 1


async def test_device_flow_continues_when_browser_cannot_open(tmp_path: Path, monkeypatch, caplog):
    config = _cli_auth_config(tmp_path, "oidc")
    _patch_device_flow(
        monkeypatch,
        authorize=_authorize_ok(),
        token=_StubResponse(payload={"access_token": "k"}),
        browser=_StubBrowser(fail=True),
    )

    with caplog.at_level(logging.DEBUG):
        assert await _authenticate_oidc_device_flow(config) == "k"

    assert "自动打开浏览器失败" in caplog.text


# ================================================================== 单轮执行


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
    config = _cli_auth_config(tmp_path, "apikey", auth_api_key_dev="dev-secret")
    monkeypatch.setenv("HARNESS_API_KEY", "dev-secret")
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
