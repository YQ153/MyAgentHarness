"""模型域配置：各 provider 的接入参数与多模态能力声明。

字段从原 ``config.AppConfig`` 的「模型」分区（原 L485–524）整体迁入，
语义与默认值保持逐字不变。
"""

from typing import Annotated

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import NoDecode

from config.parsing import parse_list_config


class LlmSettings(BaseModel):
    """模型接入域的字段。

    使用 ``pydantic-settings`` 的字段机制而非裸 ``os.getenv``：类型转换、
    缺省值与非法值拦截由框架统一处理（组合进 ``AppConfig`` 后生效）。
    """

    # 密钥类字段一律 repr=False：AppConfig 对象会出现在 pytest 断言失败输出、
    # 日志与异常上报里，默认 repr 会把明文密钥一并带出去——这条路径不报错、
    # 不告警，泄露发生时没有任何提示（回归见 tests/test_config_repr.py）。
    deepseek_api_key: str = Field(default="", repr=False)
    deepseek_api_base: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    default_model: str = "deepseek-flash"

    openai_api_key: str = Field(default="", repr=False)
    """OpenAI 的 API Key；留空则不注册 ``openai`` 别名。"""

    openai_api_base: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    anthropic_api_key: str = Field(default="", repr=False)
    """Anthropic 的 API Key；留空则不注册 ``anthropic`` 别名。"""

    anthropic_api_base: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-3-5-sonnet-latest"

    ollama_base_url: str = "http://localhost:11434"
    """本地 Ollama 服务地址；仅在显式设置 ``OLLAMA_BASE_URL`` 时注册该别名。"""

    ollama_model: str = "llama3.1"

    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_timeout: float = Field(default=60.0, gt=0.0)
    llm_max_retries: int = Field(default=2, ge=0)

    vision_model_aliases: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["openai", "anthropic"]
    )
    """哪些**模型别名**支持图片输入（多模态）。

    WHY 做成配置而不是写死在 ``ModelSpec`` 里：能力取决于**实际模型**，而模型名
    是可配置的（``OPENAI_MODEL`` 可以被指向一个纯文本模型）。写死会在「照着文档
    换了个模型」之后继续允许上传，直到构造消息时才失败——那正是本任务要消除的
    静默失败。默认值只覆盖各 provider 的官方默认模型。
    """

    @field_validator("vision_model_aliases", mode="before")
    @classmethod
    def _parse_vision_aliases(cls, value: object) -> object:
        """按逗号切分模型别名清单，口径见 ``parse_list_config``。

        WHY 与附件 MIME 分成两个 validator（原 ``_parse_csv_lists`` 按域拆分而来）：
        分隔符相同但语义不同，合并成一个 validator 会让日后其中一个改口径时
        把另一个一起改掉。

        WHY ``field`` 标注为本字段名而不是沿用旧的 ``attachment_allowed_mime_types``：
        旧实现两个字段共用一个 validator，错误信息只能带其中一个的名字——别名配错
        时报的是「附件 MIME」的问题，把排查引向完全无关的字段。拆开后各自的报错
        各说各的字段。
        """
        return parse_list_config(value, field="vision_model_aliases", separators=(",",))

    @field_validator("vision_model_aliases", mode="after")
    @classmethod
    def _normalize_aliases(cls, value: list[str]) -> list[str]:
        """归一模型别名（去空白、去重保序）；允许为空（表示没有任何多模态模型）。"""
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            candidate = str(item).strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                normalized.append(candidate)
        return normalized
