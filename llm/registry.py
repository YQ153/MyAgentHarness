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

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

_PLACEHOLDER_API_KEY = "your_api_key_here"
""".env.example 中的占位密钥；命中它说明用户尚未填写真实密钥。"""


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


@dataclass(frozen=True)
class ModelConfigProbe:
    """默认模型配置的静态探测结果。

    WHY 只做静态检查而不真正构造模型：就绪探测每秒都可能被调用，构造模型会
    读取密钥、建立连接池甚至触发 provider 侧的鉴权请求，既慢又会把探活变成
    一次真实调用。这里只回答「配置是否自洽」——别名已注册、密钥已提供、
    地址格式合法——真正的可用性由首次请求验证。
    """

    name: str
    """被探测的模型别名。"""

    registered: bool
    """该别名是否已注册。"""

    detail: str = ""
    """不可用时的人类可读原因；可用时为空串。"""

    @property
    def ok(self) -> bool:
        """配置是否可用于发起请求。"""
        return self.registered and not self.detail


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

    def probe_config(self, name: str | None = None) -> ModelConfigProbe:
        """静态探测某个模型别名的配置是否自洽（不构造模型、不发请求）。

        Args:
            name: 模型别名；``None`` 表示探测默认模型。

        Returns:
            探测结果；``ok`` 为 ``True`` 时表示别名、密钥与地址三者都可用。
        """
        key = name or self._default
        spec = self._specs.get(key)
        if spec is None:
            return ModelConfigProbe(
                name=key,
                registered=False,
                detail=f"模型 {key!r} 未注册，可选：{self.names()}",
            )

        if spec.api_key_env:
            api_key = os.getenv(spec.api_key_env, "").strip()
            if not api_key:
                return ModelConfigProbe(
                    name=key,
                    registered=True,
                    detail=f"缺少环境变量 {spec.api_key_env}，无法初始化模型 {key!r}",
                )
            if api_key == _PLACEHOLDER_API_KEY:
                return ModelConfigProbe(
                    name=key,
                    registered=True,
                    detail=f"{spec.api_key_env} 仍是占位值，请在 .env 中填写真实密钥",
                )

        try:
            _resolve_base_url(spec)
        except RuntimeError as exc:
            return ModelConfigProbe(name=key, registered=True, detail=str(exc))

        return ModelConfigProbe(name=key, registered=True)

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
        """真正构造模型实例，异常统一收敛为 RuntimeError。

        WHY 不再在此注册 HarnessProfile：注册属于装配层职责，由
        ``agent.graph.get_registry`` 在构造 registry 之前完成。放在这里会让
        ``llm`` 包反向依赖 ``agent``，形成包级循环依赖。
        """
        api_key = os.getenv(spec.api_key_env, "").strip() if spec.api_key_env else ""
        if spec.api_key_env and not api_key:
            # 入口处显式失败，优于让 SDK 在首次请求时抛出难以定位的鉴权错误
            raise RuntimeError(
                f"缺少 {spec.api_key_env} 环境变量，无法初始化模型 {spec.name!r}"
            )

        if api_key == _PLACEHOLDER_API_KEY:
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


def _ensure_env(name: str, value: str) -> None:
    """把配置里的密钥回填到进程环境变量（仅在环境变量缺失时）。

    WHY 必须回填：``pydantic-settings`` 从 ``.env`` 读到的值只进入配置对象，
    不会写回 ``os.environ``；而 provider SDK 与本模块的探测/构建都按环境
    变量取密钥。不回填就会出现「配置里有 Key，运行却报缺少环境变量」的割裂，
    且这类问题只在「密钥只写在 .env 里」的部署上暴露。

    WHY 占位值不回填：它代表用户尚未填写，回填等于把一个假密钥写进进程环境，
    后续任何组件读到的都是「有密钥」的假象。
    """
    if not name or not isinstance(value, str):
        return

    candidate = value.strip()
    if not candidate or candidate == _PLACEHOLDER_API_KEY:
        return
    if os.getenv(name, "").strip():
        return

    os.environ[name] = candidate
    logger.debug("已从配置回填环境变量 %s", name)


def _is_available(spec: ModelSpec) -> bool:
    """判断该 provider 是否具备可注册的配置。

    WHY 按配置存在性条件注册而不是无条件注册：下拉框里出现一个「点了就报
    缺少 OPENAI_API_KEY」的条目，等于把配置错误转嫁给终端用户；不注册则它
    根本不出现在选项里，而误用别名仍会被 ``get`` 的 ``KeyError`` 拦下。

    WHY 无密钥的 provider 以「显式设置了地址」为开关：本地 Ollama 的默认
    地址对本机开发是合理的，但对绝大多数部署都不可达，默认注册会让每个环境
    都多出一个连不上的选项。
    """
    if spec.api_key_env:
        api_key = os.getenv(spec.api_key_env, "").strip()
        return bool(api_key) and api_key != _PLACEHOLDER_API_KEY

    return bool(os.getenv(spec.base_url_env, "").strip())


def build_default_registry(config: AppConfig) -> ModelRegistry:
    """从配置构造注册表。

    DeepSeek 恒定注册（它是默认模型，也保证注册表非空）；OpenAI / Anthropic /
    Ollama 按配置是否提供密钥（或地址）条件注册。新增 provider 只需在此追加
    一条 ``ModelSpec``，其余代码无需改动。

    前置条件：调用方必须已注册 HarnessProfile（见 ``agent.profiles``），
    通常由装配层或 ``agent.graph.get_registry`` 完成。本函数刻意不自行注册，
    以避免 ``llm`` 包反向依赖 ``agent`` 形成包级循环。
    """
    if config is None:
        raise ValueError("config 不能为 None")

    for env_name, value in (
        ("DEEPSEEK_API_KEY", config.deepseek_api_key),
        ("OPENAI_API_KEY", config.openai_api_key),
        ("ANTHROPIC_API_KEY", config.anthropic_api_key),
    ):
        _ensure_env(env_name, value)

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

    optional_specs = (
        ModelSpec(
            name="openai",
            provider="openai",
            model=config.openai_model,
            temperature=config.llm_temperature,
            timeout=config.llm_timeout,
            max_retries=config.llm_max_retries,
            api_key_env="OPENAI_API_KEY",
            base_url_env="OPENAI_API_BASE",
            base_url_default=config.openai_api_base,
        ),
        ModelSpec(
            name="anthropic",
            provider="anthropic",
            model=config.anthropic_model,
            temperature=config.llm_temperature,
            timeout=config.llm_timeout,
            max_retries=config.llm_max_retries,
            api_key_env="ANTHROPIC_API_KEY",
            base_url_env="ANTHROPIC_API_BASE",
            base_url_default=config.anthropic_api_base,
        ),
        ModelSpec(
            name="ollama",
            provider="ollama",
            model=config.ollama_model,
            temperature=config.llm_temperature,
            timeout=config.llm_timeout,
            max_retries=config.llm_max_retries,
            base_url_env="OLLAMA_BASE_URL",
            base_url_default=config.ollama_base_url,
        ),
    )

    for spec in optional_specs:
        if _is_available(spec):
            specs.append(spec)
        else:
            logger.info(
                "模型 %s 未注册：%s 未提供",
                spec.name,
                spec.api_key_env or spec.base_url_env,
            )

    default = config.default_model
    if default not in {spec.name for spec in specs}:
        logger.warning(
            "配置的默认模型 %s 未注册（缺少对应密钥或地址），回落到 %s",
            default,
            specs[0].name,
        )
        default = specs[0].name

    return ModelRegistry(specs=specs, default=default)
