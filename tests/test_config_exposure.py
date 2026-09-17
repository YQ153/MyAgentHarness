"""未认证暴露自检（技术债 D-8）。

WHY 需要它：服务默认绑 ``127.0.0.1``，此时没有认证也无所谓；一旦改绑 ``0.0.0.0``
而 ``AUTH_MODE`` 仍是默认的 ``disabled``，任何能访问该地址的人都能直接驱动
``execute`` 工具。这个状态不报错、不崩溃、不影响任何功能，只会安静地存在——
所以只能靠显式断言把它钉住。

覆盖三层，缺一层就存在绕过路径：
1. 判定函数 ``is_loopback_host``——误报会训练用户忽略告警，漏报则等于没有检查；
2. 配置方法 ``warn_if_unauthenticated_exposure``——认证开启后不应再告警；
3. 两条入口的接线（``main._run_web`` / ``main._run_cli``）——防止「检查写了但没
   接上」，尤其是 ``--host`` 覆盖配置这条绕过路径。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from config import AppConfig, is_loopback_host
from tests.conftest import make_config

_AUTHED_OVERRIDES: dict[str, str] = {
    "auth_mode": "apikey",
    "auth_session_secret": "s" * 32,
}
"""启用认证所需的最小配置：apikey 模式要求会话密钥不少于 32 字节。"""


# ------------------------------------------------------------------ 判定函数


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.53", "::1", "[::1]", "localhost", "LOCALHOST", " 127.0.0.1 "],
)
def test_loopback_recognized(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.10", "harness.internal", "", "   ", None],
)
def test_non_loopback_treated_as_exposed(host: str | None) -> None:
    """无法证明是本机的一律按「可能对外」处理，宁可误报不可漏报。"""
    assert is_loopback_host(host) is False


def test_non_string_host_rejected() -> None:
    with pytest.raises(ValueError, match="host"):
        is_loopback_host(123)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 自检方法


def test_loopback_bind_stays_quiet(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """默认部署（绑回环）不该产生告警：噪音会盖掉真正危险的那一次。"""
    config = make_config(tmp_path, host="127.0.0.1", auth_mode="disabled")
    caplog.clear()

    with caplog.at_level(logging.ERROR):
        warned = config.warn_if_unauthenticated_exposure()

    assert warned is False
    assert caplog.records == []


def test_exposed_without_auth_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = make_config(tmp_path, host="0.0.0.0", auth_mode="disabled")
    caplog.clear()

    with caplog.at_level(logging.ERROR):
        warned = config.warn_if_unauthenticated_exposure()

    assert warned is True
    record = next(r for r in caplog.records if r.levelno == logging.ERROR)
    assert "AUTH_MODE=disabled" in record.getMessage()


def test_exposed_but_authenticated_stays_quiet(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """已启用认证就不属于「未认证暴露」；此处也告警会让告警失去指向性。"""
    config = make_config(tmp_path, host="0.0.0.0", **_AUTHED_OVERRIDES)
    caplog.clear()

    with caplog.at_level(logging.ERROR):
        warned = config.warn_if_unauthenticated_exposure()

    assert warned is False
    assert caplog.records == []


def test_effective_host_overrides_config(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """配置绑回环、命令行改绑对外时仍须告警——这是最容易被绕过的路径。"""
    config = make_config(tmp_path, host="127.0.0.1", auth_mode="disabled")
    caplog.clear()

    with caplog.at_level(logging.ERROR):
        warned = config.warn_if_unauthenticated_exposure("0.0.0.0")

    assert warned is True
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_non_string_effective_host_rejected(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    with pytest.raises(ValueError, match="effective_host"):
        config.warn_if_unauthenticated_exposure(8000)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 入口接线


def test_run_web_checks_effective_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``main.py web --host 0.0.0.0`` 必须告警：配置绑回环不构成豁免。

    WHY 拦掉 create_app 与 uvicorn.run：本用例验证的是接线，不是服务器本身；
    真起一个监听端口会让测试变慢且引入无关的失败面。
    """
    import main as main_module

    monkeypatch.setattr("interfaces.web.app.create_app", lambda config: object())
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    config = make_config(tmp_path, host="127.0.0.1", auth_mode="disabled")
    caplog.clear()

    with caplog.at_level(logging.ERROR):
        exit_code = main_module._run_web(config, "0.0.0.0", 8000)

    assert exit_code == 0
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_run_cli_warns_for_exposed_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """CLI 不监听端口，但同一份 .env 通常也用于 Web 形态，操作者应能提前看见。"""
    import main as main_module

    async def _fake_run_cli(config: AppConfig, model_name: str | None = None) -> int:
        return 0

    monkeypatch.setattr("interfaces.cli.run_cli", _fake_run_cli)
    config = make_config(tmp_path, host="0.0.0.0", auth_mode="disabled")
    caplog.clear()

    with caplog.at_level(logging.ERROR):
        exit_code = main_module._run_cli(config, None)

    assert exit_code == 0
    assert any(r.levelno == logging.ERROR for r in caplog.records)
