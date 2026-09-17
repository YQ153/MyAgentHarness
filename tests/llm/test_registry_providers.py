"""多 Provider 注册的回归测试。

覆盖面：DeepSeek 恒定注册、OpenAI/Anthropic 按密钥存在性条件注册（含占位值
不算已配置）、Ollama 按显式地址注册、默认模型回落、配置到环境变量的回填、
缺失密钥的快速失败，以及 ``with_model`` 视图共享缓存。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from llm.registry import (
    ModelRegistry,
    ModelSpec,
    _ensure_env,
    _is_available,
    build_default_registry,
)
from tests.conftest import make_config


def _config(tmp_path: Path, **overrides: object):
    return make_config(tmp_path, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def _clean_provider_env(monkeypatch):
    """清掉本机可能存在的 provider 环境变量，避免测试结果受机器影响。"""
    for name in (
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_BASE",
        "OLLAMA_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------ 条件注册


def test_deepseek_always_registered(tmp_path: Path, _clean_provider_env):
    registry = build_default_registry(_config(tmp_path))

    # WHY 无条件注册：它是默认模型，也是「注册表非空」的兜底条目
    assert registry.names() == ["deepseek-flash"]
    assert registry.default_name == "deepseek-flash"


def test_openai_registered_when_key_present(tmp_path: Path, _clean_provider_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    registry = build_default_registry(_config(tmp_path))

    assert registry.names() == ["deepseek-flash", "openai"]
    assert registry.probe_config("openai").ok is True


def test_openai_skipped_when_key_missing(tmp_path: Path, _clean_provider_env):
    registry = build_default_registry(_config(tmp_path))

    assert "openai" not in registry.names()
    # 未注册的别名必须是明确的未注册，而不是「已注册但不可用」
    probe = registry.probe_config("openai")
    assert probe.registered is False
    assert probe.ok is False


def test_placeholder_key_does_not_register(tmp_path: Path, _clean_provider_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "your_api_key_here")

    registry = build_default_registry(_config(tmp_path))

    assert "anthropic" not in registry.names()


def test_anthropic_registered_when_key_present(tmp_path: Path, _clean_provider_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    registry = build_default_registry(_config(tmp_path))

    assert "anthropic" in registry.names()
    assert registry.describe() == [
        {
            "name": "anthropic",
            "provider": "anthropic",
            "model": "claude-3-5-sonnet-latest",
        },
        {"name": "deepseek-flash", "provider": "deepseek", "model": "deepseek-flash"},
    ]


def test_ollama_registered_only_when_url_set(tmp_path: Path, _clean_provider_env, monkeypatch):
    assert "ollama" not in build_default_registry(_config(tmp_path)).names()

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    registry = build_default_registry(_config(tmp_path))

    assert "ollama" in registry.names()
    # 本地 provider 不需要密钥，探测不应把它判成不可用
    assert registry.probe_config("ollama").ok is True


def test_all_providers_can_coexist(tmp_path: Path, _clean_provider_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")

    registry = build_default_registry(_config(tmp_path))

    assert registry.names() == [
        "anthropic",
        "deepseek-flash",
        "ollama",
        "openai",
    ]


# ------------------------------------------------------------------ 默认模型


def test_default_model_falls_back_when_not_registered(tmp_path: Path, _clean_provider_env):
    """WHY 覆盖这条：``DEFAULT_MODEL=openai`` 但没配 Key 时，
    若不做回落，注册表构造会直接抛 KeyError，整个应用起不来。"""
    registry = build_default_registry(_config(tmp_path, default_model="openai"))

    assert registry.default_name == "deepseek-flash"


def test_default_model_kept_when_registered(tmp_path: Path, _clean_provider_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")

    registry = build_default_registry(_config(tmp_path, default_model="openai"))

    assert registry.default_name == "openai"


# ------------------------------------------------------------------ 配置回填


def test_config_key_mirrored_to_env(tmp_path: Path, _clean_provider_env, monkeypatch):
    """WHY 覆盖回填：密钥只写在 .env 时，pydantic-settings 不会写回
    os.environ，provider SDK 与探测都按环境变量取值，不回填就永远缺 Key。"""
    registry = build_default_registry(_config(tmp_path, openai_api_key="sk-from-config"))

    assert "openai" in registry.names()
    assert registry.probe_config("openai").ok is True


def test_placeholder_in_config_is_not_mirrored(tmp_path: Path, _clean_provider_env):
    build_default_registry(_config(tmp_path, openai_api_key="your_api_key_here"))

    # 占位值回填会制造「有密钥」的假象，必须原样丢弃
    assert os.getenv("OPENAI_API_KEY") is None


def test_ensure_env_keeps_existing_value(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real")

    _ensure_env("DEEPSEEK_API_KEY", "sk-from-config")

    assert os.getenv("DEEPSEEK_API_KEY") == "sk-real"


@pytest.mark.parametrize("value", ["", "   ", "your_api_key_here", None])
def test_ensure_env_ignores_blank_and_placeholder(monkeypatch, value):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    _ensure_env("OPENAI_API_KEY", value)

    assert os.getenv("OPENAI_API_KEY") is None


# ------------------------------------------------------------------ 快速失败


def _spec(**overrides: object) -> ModelSpec:
    params: dict[str, object] = {
        "name": "probe-target",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
        "base_url_env": "OPENAI_API_BASE",
        "base_url_default": "https://api.openai.com/v1",
    }
    params.update(overrides)
    return ModelSpec(**params)  # type: ignore[arg-type]


def test_missing_key_fails_fast_on_get(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    registry = ModelRegistry(specs=[_spec()], default="probe-target")

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        registry.get("probe-target")


def test_placeholder_key_fails_fast_on_get(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "your_api_key_here")
    registry = ModelRegistry(specs=[_spec()], default="probe-target")

    with pytest.raises(RuntimeError, match="占位值"):
        registry.get("probe-target")


def test_invalid_base_url_fails_fast_on_get(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("OPENAI_API_BASE", "not-a-url")
    registry = ModelRegistry(specs=[_spec()], default="probe-target")

    with pytest.raises(RuntimeError, match="OPENAI_API_BASE"):
        registry.get("probe-target")


def test_is_available_requires_explicit_url_for_keyless_provider(monkeypatch):
    spec = _spec(api_key_env="", base_url_env="OLLAMA_BASE_URL", base_url_default="")

    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert _is_available(spec) is False

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    assert _is_available(spec) is True


# ------------------------------------------------------------------ 视图共享缓存


def test_with_model_shares_cache(monkeypatch):
    """WHY 覆盖共享：切换模型若重建实例，来回切换会反复初始化连接，
    并可能突破 provider 侧限流。"""
    built: list[str] = []

    def fake_build(self: ModelRegistry, spec: ModelSpec) -> str:
        built.append(spec.name)
        return f"model:{spec.name}"

    monkeypatch.setattr(ModelRegistry, "_build", fake_build)
    registry = ModelRegistry(
        specs=[
            _spec(name="a", api_key_env="", base_url_env="", base_url_default=""),
            _spec(name="b", api_key_env="", base_url_env="", base_url_default=""),
        ],
        default="a",
    )

    first = registry.get("a")
    view = registry.with_model("b")
    second = view.get("b")

    # 视图的默认模型变了，但缓存是同一份
    assert view.default_name == "b"
    assert registry.default_name == "a"
    assert registry.get("a") is first
    assert view.get("a") is first
    assert second == "model:b"
    assert built == ["a", "b"]


def test_with_model_rejects_unknown_name(monkeypatch):
    registry = ModelRegistry(
        specs=[_spec(name="a", api_key_env="", base_url_env="", base_url_default="")],
        default="a",
    )

    with pytest.raises(KeyError):
        registry.with_model("missing")
