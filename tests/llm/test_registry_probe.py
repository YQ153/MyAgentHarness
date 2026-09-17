"""模型配置静态探测的回归测试。

WHY 单独覆盖这个探测：就绪性判定依赖它区分「别名没注册」「密钥没配」
「地址写错」三种故障，而这三种的修法完全不同；探测若退化成「永远通过」，
就绪端点就形同虚设。
"""

from __future__ import annotations

import pytest

from llm.registry import ModelRegistry, ModelSpec


def _spec(**overrides: object) -> ModelSpec:
    params: dict[str, object] = {
        "name": "deepseek-flash",
        "provider": "deepseek",
        "model": "deepseek-flash",
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_API_BASE",
        "base_url_default": "https://api.deepseek.com",
    }
    params.update(overrides)
    return ModelSpec(**params)  # type: ignore[arg-type]


def _registry(spec: ModelSpec) -> ModelRegistry:
    return ModelRegistry(specs=[spec], default=spec.name)


# ------------------------------------------------------------------ 通过路径


def test_probe_ok_when_key_present(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-key")
    monkeypatch.delenv("DEEPSEEK_API_BASE", raising=False)
    registry = _registry(_spec())

    probe = registry.probe_config()

    assert probe.ok is True
    assert probe.registered is True
    assert probe.detail == ""


def test_probe_default_alias_when_name_omitted(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-key")
    registry = _registry(_spec())

    probe = registry.probe_config(None)

    assert probe.name == "deepseek-flash"
    assert probe.ok is True


def test_probe_skips_key_check_for_keyless_provider():
    """Ollama 这类本地 provider 不需要密钥，探测不应把它判成不可用。"""
    registry = _registry(_spec(api_key_env="", base_url_env="", base_url_default=""))

    probe = registry.probe_config()

    assert probe.ok is True


# ------------------------------------------------------------------ 降级路径


def test_probe_reports_unregistered_alias(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-key")
    registry = _registry(_spec())

    probe = registry.probe_config("gpt-4o")

    assert probe.ok is False
    assert probe.registered is False
    assert "未注册" in probe.detail


def test_probe_reports_missing_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    registry = _registry(_spec())

    probe = registry.probe_config()

    assert probe.ok is False
    assert probe.registered is True
    assert "DEEPSEEK_API_KEY" in probe.detail


def test_probe_reports_placeholder_api_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "your_api_key_here")
    registry = _registry(_spec())

    probe = registry.probe_config()

    assert probe.ok is False
    assert "占位值" in probe.detail


def test_probe_reports_invalid_base_url(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-key")
    monkeypatch.setenv("DEEPSEEK_API_BASE", "not-a-url")
    registry = _registry(_spec())

    probe = registry.probe_config()

    assert probe.ok is False
    assert "DEEPSEEK_API_BASE" in probe.detail


def test_probe_does_not_construct_model(monkeypatch):
    """WHY 本用例是探测的性能契约：探活每秒都可能被调用，
    一旦退化成构造模型，就会在每次探活时建立连接甚至发一次鉴权请求。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-key")

    def forbidden_build(self: ModelRegistry, spec: ModelSpec) -> object:
        raise AssertionError("探测不得构造模型")

    monkeypatch.setattr(ModelRegistry, "_build", forbidden_build)
    registry = _registry(_spec())

    assert registry.probe_config().ok is True


@pytest.mark.parametrize("name", ["", None])
def test_probe_normalizes_blank_name_to_default(monkeypatch, name):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-key")
    registry = _registry(_spec())

    probe = registry.probe_config(name)

    assert probe.name == "deepseek-flash"
