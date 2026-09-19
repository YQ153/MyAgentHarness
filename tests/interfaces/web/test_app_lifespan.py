"""Web 应用 lifespan 的退出路径回归测试。

WHY 单独成文件：同目录其它用例都刻意「只挂业务路由、不起真实应用」（见各自的 WHY），
于是 ``create_app`` 的 lifespan **从未被测试网走到**。上一个提交（移除 OIDC / Authentik）
删掉了唯一的共享 httpx 客户端，却把调用点留成对一个**不存在**的函数的调用——测试因此
全绿，直到真人启动服务才在退出路径上炸成 ``NameError``；更糟的是它抛在
``build_app_context`` 的退出过程中，各存储的 ``finally`` 会把它记成「初始化失败」，
把排查引向与真实原因无关的方向。本文件补上这一格。

WHY 直接驱动 ``_lifespan`` 而不是起 ``TestClient``：本组要验的就是这个异步上下文管理器
本身的起停次序与异常吞并语义，FastAPI 只是它的调用方；起真实应用会连带装配数据库、
模型与图，把失败面扩大到与断言无关的地方。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from interfaces.web import app as app_module
from interfaces.web.app import _lifespan
from tests.conftest import make_config

_LOGGER_NAME = "interfaces.web.app"


class _RecordingWorker:
    """后台任务替身：记录 start / stop 的调用次序，并可在指定阶段抛错。

    WHY 记录次序而不是只记「停过」：退出段的两个 ``if`` 是有顺序的（治理先于清理），
    顺序颠倒会让最后一轮巡检在已关闭的连接上写审计——那正是这段代码注释所声明的理由，
    必须有断言守住它，否则注释迟早与代码分叉。
    """

    def __init__(
        self,
        name: str,
        order: list[str],
        *,
        fail_on_start: bool = False,
        fail_on_stop: bool = False,
    ) -> None:
        self._name = name
        self._order = order
        self._fail_on_start = fail_on_start
        self._fail_on_stop = fail_on_stop

    def start(self) -> None:
        """启动任务；``fail_on_start`` 为真时抛错。"""
        if self._fail_on_start:
            raise RuntimeError(f"{self._name} 启动失败")
        self._order.append(f"start:{self._name}")

    async def stop(self) -> None:
        """停止任务；``fail_on_stop`` 为真时先记账再抛错。"""
        self._order.append(f"stop:{self._name}")
        if self._fail_on_stop:
            raise RuntimeError(f"{self._name} 停止失败")


def _stub_context() -> SimpleNamespace:
    """``AppContext`` 的替身：lifespan 只做属性转发，不需要真实现。"""
    return SimpleNamespace(
        audit_store=object(),
        api_key_store=object(),
        threads=object(),
        workspace=object(),
        runs=object(),
        catalog=object(),
        health=object(),
        usage=object(),
        tools=object(),
        memories=object(),
    )


def _patch_lifespan_deps(
    monkeypatch: pytest.MonkeyPatch,
    context: Any,
    order: list[str],
    *,
    retention_fail_start: bool = False,
    governance_fail_start: bool = False,
    governance_fail_stop: bool = False,
) -> None:
    """把 lifespan 取用的四个装配入口换成替身，并按需注入故障。

    WHY 连 ``build_app_context`` 一起换：真实装配会建库、连模型、编译图；本组要验的是
    lifespan 自己的起停与异常语义，替换它才能让失败直接指向 lifespan。
    """

    @asynccontextmanager
    async def fake_context(config: Any) -> AsyncIterator[Any]:
        yield context

    monkeypatch.setattr(app_module, "build_app_context", fake_context)
    monkeypatch.setattr(app_module, "build_rate_limiter", lambda config: object())
    monkeypatch.setattr(
        app_module,
        "build_audit_retention_worker",
        lambda config, store: _RecordingWorker(
            "retention", order, fail_on_start=retention_fail_start
        ),
    )
    monkeypatch.setattr(
        app_module,
        "build_run_governance_worker",
        lambda config, runs: _RecordingWorker(
            "governance",
            order,
            fail_on_start=governance_fail_start,
            fail_on_stop=governance_fail_stop,
        ),
    )


async def test_lifespan_shutdown_runs_to_completion(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """退出路径必须能跑完：这里曾调用一个不存在的 ``_close_background_workers()``。"""
    config = make_config(tmp_path)
    context = _stub_context()
    order: list[str] = []
    _patch_lifespan_deps(monkeypatch, context, order)
    app = SimpleNamespace(state=SimpleNamespace(config=config))

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        async with _lifespan(app):  # noqa: SLF001 本组用例的被测对象就是它
            # 启动段必须把上下文铺到 app.state：路由层只从这里取依赖
            assert app.state.context is context
            assert app.state.runs is context.runs
            assert app.state.memories is context.memories

    assert order == [
        "start:retention",
        "start:governance",
        "stop:governance",
        "stop:retention",
    ]
    # 末尾这行是「退出段整段执行完」的凭证：它排在曾经抛 NameError 的那一句之后
    assert "Web 服务已停止" in caplog.text


async def test_worker_start_failure_does_not_block_startup(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """审计清理启动失败只记日志：它是旁路能力，不该让服务起不来。"""
    config = make_config(tmp_path)
    context = _stub_context()
    order: list[str] = []
    _patch_lifespan_deps(monkeypatch, context, order, retention_fail_start=True)
    app = SimpleNamespace(state=SimpleNamespace(config=config))

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        async with _lifespan(app):
            # 启动段继续往下走：治理任务照常启动，路由照常拿到依赖
            assert "start:governance" in order
            assert app.state.runs is context.runs

    assert "start:retention" not in order
    assert "审计保留清理任务启动失败" in caplog.text
    assert "Web 服务已停止" in caplog.text


async def test_worker_stop_failure_does_not_block_the_other(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """治理停止失败时清理任务仍须被停掉：否则它会攥着 audit_store 连接继续活。"""
    config = make_config(tmp_path)
    order: list[str] = []
    _patch_lifespan_deps(monkeypatch, _stub_context(), order, governance_fail_stop=True)
    app = SimpleNamespace(state=SimpleNamespace(config=config))

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        async with _lifespan(app):
            pass

    assert "stop:governance" in order
    assert "stop:retention" in order
    assert "运行治理任务停止失败" in caplog.text
    assert "Web 服务已停止" in caplog.text
