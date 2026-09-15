"""模型注册表：多 provider 注册、懒加载与并发安全切换。

设计要点：
- **懒加载**：模型初始化会读取 API Key 并可能触发鉴权探测，启动时一次性
  构造所有 provider 会让某个缺失 Key 的配置直接拖垮整个应用。
- **线程安全**：Web 服务是多线程环境，并发首次请求同一模型若重复初始化，
  既浪费连接也可能突破 provider 侧限流。
- **provider 无关**：调用方只按名字取模型，切换 provider 不需要改装配代码。
"""

from __future__ import annotations

import copy
import logging
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from agent.profiles import ensure_profiles_registered

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    """一个可切换的模型条目。"""

    name: str
    """用户可见的模型别名，切换模型时用这个值。"""

    provider: str
    """langchain 的 provider 标识，如 ``deepseek`` / ``openai`` / ``anthropic``。"""

    model: str
    """provider 侧的原始模型名。"""

    temperature: float = 0.0
    timeout: float = 60.0
    max_retries: int = 2

    api_key_env: str = ""
    """读取 API Key 的环境变量名；留空表示该 provider 不需要显式 Key。"""

    base_url_env: str = ""
    """读取自定义 API 地址的环境变量名；留空表示只用官方地址。"""

    base_url_default: str = ""
    """未设置 ``base_url_env`` 时使用的官方地址。"""


def _resolve_base_url(spec: ModelSpec) -> str:
    """解析 API 地址并返回规范化结果。

    WHY 在入口处校验 URL：若交给 SDK，非法地址要到首次请求时才暴露，
    且错误栈通常指向网络连接层，很难联想到配置项写错。
    """
    default_url = spec.base_url_default
    if not spec.base_url_env:
        return default_url

    raw = os.getenv(spec.base_url_env, "").strip()
    if not raw:
        return default_url

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(
            f"{spec.base_url_env} 不是合法的 HTTP(S) 地址：{raw!r}"
        )

    # 去掉结尾斜杠，避免与 SDK 内部拼接出 //chat/completions
    return raw.rstrip("/")


def _read_effective_base_url(llm: object) -> str | None:
    """读回模型实例上真正生效的地址，用于自检配置是否被采纳。

    WHY 需要自检：不同版本的 langchain provider 包字段名不同，个别版本会
    静默忽略未知 kwargs 并回落到默认地址；这里必须报警而不是让请求打到
    非预期的服务端。
    """
    for attr in ("api_base", "deepseek_api_base", "openai_api_base", "base_url"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value.rstrip("/")
    return None


class ModelRegistry:
    """按名字提供模型实例，内置懒加载缓存。"""

    def __init__(self, specs: list[ModelSpec], default: str) -> None:
        if not specs:
            raise ValueError("specs 不能为空，至少需要注册一个模型")
        if not default:
            raise ValueError("default 不能为空")

        self._specs: dict[str, ModelSpec] = {spec.name: spec for spec in specs}
        if default not in self._specs:
            raise KeyError(f"默认模型 {default!r} 不在注册表中：{sorted(self._specs)}")

        self._default = default
        self._cache: dict[str, BaseChatModel] = {}
        # WHY 独立一把锁而非直接用 registry 对象自身：避免外部误用 synchronized
        # 语义，也让锁的职责一目了然。
        self._lock = threading.Lock()

    def names(self) -> list[str]:
        """返回可用模型别名列表，供前端下拉框使用。"""
        return sorted(self._specs)

    @property
    def default_name(self) -> str:
        return self._default

    def describe(self) -> list[dict[str, str]]:
        """返回模型的展示信息，不含密钥。"""
        return [
            {"name": spec.name, "provider": spec.provider, "model": spec.model}
            for spec in sorted(self._specs.values(), key=lambda item: item.name)
        ]

    def get(self, name: str | None = None) -> BaseChatModel:
        """按别名取模型；``None`` 表示取默认模型。"""
        key = name or self._default
        if key not in self._specs:
            raise KeyError(f"未注册的模型 {key!r}，可选：{self.names()}")

        # 双检锁：快路径无锁返回，慢路径加锁后二次确认，避免重复初始化
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

            spec = self._specs[key]
            model = self._build(spec)
            self._cache[key] = model
            logger.info("模型就绪：%s (%s:%s)", key, spec.provider, spec.model)
            return model

    def with_model(self, name: str | None) -> ModelRegistry:
        """返回一个默认模型不同的视图，共享同一份缓存。

        WHY 共享缓存：切换模型不应丢弃已初始化的连接，否则来回切换会反复重建。
        """
        key = name or self._default
        if key not in self._specs:
            raise KeyError(f"未注册的模型 {key!r}，可选：{self.names()}")

        # WHY 浅拷贝而非重新构造：新视图必须共享同一个缓存字典和锁对象，
        # 否则切换后又会重新初始化模型，违背共享连接的初衷。
        view = copy.copy(self)
        view._default = key  # noqa: SLF001
        return view

    def _build(self, spec: ModelSpec) -> BaseChatModel:
        """真正构造模型实例，异常统一收敛为 RuntimeError。"""
        ensure_profiles_registered()

        api_key = os.getenv(spec.api_key_env, "").strip() if spec.api_key_env else ""
        if spec.api_key_env and not api_key:
            # 入口处显式失败，优于让 SDK 在首次请求时抛出难以定位的鉴权错误
            raise RuntimeError(
                f"缺少 {spec.api_key_env} 环境变量，无法初始化模型 {spec.name!r}"
            )

        if api_key == "your_api_key_here":
            raise RuntimeError(
                f"{spec.api_key_env} 仍是占位值，请在 .env 中填写真实密钥"
            )

        base_url = _resolve_base_url(spec)

        kwargs: dict[str, object] = {
            "model": spec.model,
            "model_provider": spec.provider,
            "temperature": spec.temperature,
            "timeout": spec.timeout,
            "max_retries": spec.max_retries,
        }
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url

        try:
            llm = init_chat_model(**kwargs)
        except ImportError as exc:
            # 依赖缺失必须单独分出来：它的修法（装包）与网络/鉴权完全不同
            logger.exception("模型 %s 缺少 provider 依赖", spec.name)
            raise RuntimeError(
                f"模型 {spec.name} 需要额外的 provider 包，请检查依赖安装"
            ) from exc
        except Exception as exc:
            logger.exception("模型 %s 初始化失败", spec.name)
            raise RuntimeError(
                f"模型 {spec.name} 初始化失败，请检查 API Key 与网络连通性"
            ) from exc

        if base_url:
            effective = _read_effective_base_url(llm)
            if effective and effective != base_url:
                logger.warning(
                    "API 地址未按预期生效：期望 %s，实际 %s", base_url, effective
                )
            else:
                logger.info("API 地址已生效：%s", base_url)

        return llm


def build_default_registry(config: AppConfig) -> ModelRegistry:
    """从配置构造注册表。

    当前默认只注册 DeepSeek；新增 provider 只需在此追加一条 ``ModelSpec``，
    其余代码无需改动。
    """
    if config is None:
        raise ValueError("config 不能为 None")

    specs = [
        ModelSpec(
            name="deepseek-flash",
            provider="deepseek",
            model=config.deepseek_model,
            temperature=config.llm_temperature,
            timeout=config.llm_timeout,
            max_retries=config.llm_max_retries,
            api_key_env="DEEPSEEK_API_KEY",
            base_url_env="DEEPSEEK_API_BASE",
            base_url_default=config.deepseek_api_base,
        ),
    ]

    default = config.default_model
    if default not in {spec.name for spec in specs}:
        logger.warning("配置的默认模型 %s 未注册，回落到 deepseek-flash", default)
        default = "deepseek-flash"

    return ModelRegistry(specs=specs, default=default)
