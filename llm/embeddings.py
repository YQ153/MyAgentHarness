"""嵌入后端：把文本变成向量，供知识库的索引与检索使用。

WHY 抽象成协议而不是直接调某个库：同一份知识库要能在「容器内模型服务」与「本机
自托管」两种部署下工作，而二者的差别只在**怎么拿到向量**——切分、存储、检索三段
完全一致。把差异收在一个 ``embed()`` 后面，那三段就不需要任何分支。

WHY 装配必须惰性：默认档位是 ``none``（见 ``config.EmbeddingBackendKind``），而模型
相关依赖体积大、导入慢；「没启用知识库」的部署不该为此付出代价，因此本模块在导入期
不构造任何客户端、不加载任何模型。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Sequence, runtime_checkable

import httpx

from llm.embed_process import EmbedProcessClient, default_embed_python

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

_EMBEDDINGS_PATH = "/v1/embeddings"
"""OpenAI 兼容的嵌入端点路径。"""

_SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "embed_server.py"
"""承载模型的瘦服务脚本位置。

WHY 从本模块位置推导而不是做成配置项：它是本项目自带的**实现细节**，不是部署参数；
做成配置只会多出一个「路径写错导致子进程起不来」的失败面。各机器真正不同的是解释器
路径，那一项才对应 ``EMBEDDING_PYTHON``。
"""


class EmbeddingError(RuntimeError):
    """嵌入调用失败（服务不可达、响应形状不符、子进程异常）。"""


@runtime_checkable
class EmbeddingBackend(Protocol):
    """嵌入后端协议。

    WHY 用结构性协议而不是抽象基类：子进程档位的实现只依赖 stdlib，让它去继承一个
    定义在别处的基类，会平白建立一条导入关系；协议只约束「有哪些成员」。
    """

    name: str
    """后端标识，用于日志与能力公示。"""

    dims: int
    """向量维度；必须与 ``config.embedding_dims`` 一致。"""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量嵌入，返回顺序与入参一致。"""

    async def aclose(self) -> None:
        """释放连接或子进程。"""


class OpenAICompatEmbeddings:
    """通过 ``POST /v1/embeddings`` 调用任意兼容服务。

    WHY 自己写 HTTP 而不复用 provider SDK：嵌入端点极小（一个 POST、两个字段），而
    SDK 会把聊天模型那一整套参数、重试与流式逻辑一并带进来；更要紧的是，本档位实际
    要接的往往是自建的 TEI / Ollama —— 它们兼容这个端点，却不一定兼容各家 SDK 的
    隐式约定（默认地址、请求头、响应包装）。容器内加载模型就走这一档。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dims: int,
        api_key: str = "",
        timeout: float = 30.0,
        batch_size: int = 32,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """构造后端。

        Args:
            base_url: 服务地址，**不含** ``/v1``（由本类拼接）。
            model: 请求体里的 ``model`` 字段；服务端据此定位要用的模型。
            dims: 期望的向量维度。
            api_key: 服务密钥；本地服务通常不需要。
            timeout: 单次请求超时秒数。
            batch_size: 单次请求的文本条数上限。
            client: 注入的 HTTP 客户端；``None`` 表示自建并自行关闭。

        Raises:
            EmbeddingError: ``base_url`` 为空或 ``dims`` 非正数。
        """
        if not base_url.strip():
            raise EmbeddingError("openai-compat 后端需要 base_url")
        if dims < 1:
            raise EmbeddingError(f"dims 必须为正整数，实际：{dims}")

        self.name = f"openai-compat:{model}"
        self.dims = dims

        self._url = base_url.rstrip("/") + _EMBEDDINGS_PATH
        self._model = model
        self._api_key = api_key.strip()
        self._timeout = timeout
        self._batch_size = batch_size
        self._client = client
        self._owns_client = client is None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """分批嵌入，顺序与入参一致。

        Raises:
            EmbeddingError: 请求失败、响应非法或维度不符。
        """
        if not texts:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def aclose(self) -> None:
        """关闭自建的 HTTP 客户端；注入的客户端归调用方管理。"""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """嵌入一批文本。

        Raises:
            EmbeddingError: 请求失败、响应非法或维度不符。
        """
        client = self._ensure_client()
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

        try:
            response = await client.post(
                self._url,
                json={"model": self._model, "input": batch},
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"嵌入服务请求失败：{type(exc).__name__}: {exc}") from exc

        if response.status_code >= 400:
            raise EmbeddingError(
                f"嵌入服务返回 {response.status_code}：{response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise EmbeddingError(f"嵌入服务返回的不是 JSON：{response.text[:200]}") from exc

        return self._parse_items(payload, len(batch))

    def _parse_items(self, payload: object, expected_count: int) -> list[list[float]]:
        """从响应体中取出向量，并校验条数、顺序与维度。

        Raises:
            EmbeddingError: 缺少 ``data``、条数不符、缺少 ``index`` 或维度不符。
        """
        if not isinstance(payload, dict):
            raise EmbeddingError(f"嵌入响应不是对象：{str(payload)[:200]}")

        items = payload.get("data")
        if not isinstance(items, list):
            raise EmbeddingError(f"嵌入响应缺少 data 数组：{str(payload)[:200]}")

        # WHY 按 index 排序而不是信任返回顺序：OpenAI 兼容实现都带 index，而「返回顺序
        # 与入参一致」只是一条约定。批量嵌入一旦错位，写进索引的会是「A 的向量配 B 的
        # 文本」，而且**不会报错**——检索结果会长期悄悄歪掉，直到有人逐条核对才发现。
        try:
            ordered = sorted(items, key=lambda item: int(item["index"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(
                f"嵌入响应缺少可排序的 index 字段：{str(items)[:200]}"
            ) from exc

        if len(ordered) != expected_count:
            raise EmbeddingError(
                f"嵌入条数不符：请求 {expected_count} 条，返回 {len(ordered)} 条"
            )

        vectors: list[list[float]] = []
        for item in ordered:
            raw = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(raw, list):
                raise EmbeddingError(f"嵌入项缺少 embedding 数组：{str(item)[:200]}")
            vector = [float(value) for value in raw]
            if len(vector) != self.dims:
                raise EmbeddingError(
                    f"向量维度与配置不符：期望 {self.dims}，实际 {len(vector)}。"
                    "换过模型必须同步修改 EMBEDDING_DIMS 并重建索引"
                )
            vectors.append(vector)
        return vectors

    def _ensure_client(self) -> httpx.AsyncClient:
        """惰性创建 HTTP 客户端。"""
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client


def build_embeddings(config: AppConfig) -> EmbeddingBackend | None:
    """按配置装配嵌入后端。

    WHY 用 ``None`` 表示 ``none`` 档位，而不是造一个「调用即报错」的哑后端：``none``
    是一等档位——知识库在它下面仍然可用（关键词检索），所以「没有嵌入」不是故障，而是
    检索路径上的一次分支。用哑对象表达它，会让每个调用点都要先问「这是不是那个哑对象」，
    并且把「设计上的降级」写成了「运行时的异常」。

    Args:
        config: 应用配置。

    Returns:
        已装配的后端；未启用时返回 ``None``。

    Raises:
        EmbeddingError: 档位取值未知，或该档位所需的参数不合法。
    """
    kind = str(config.embedding_backend)

    if kind == "none":
        logger.info("嵌入后端未启用（EMBEDDING_BACKEND=none）：知识库只做关键词检索")
        return None

    if kind == "openai-compat":
        backend: EmbeddingBackend = OpenAICompatEmbeddings(
            base_url=config.embedding_base_url,
            model=config.embedding_model,
            dims=config.embedding_dims,
            api_key=config.embedding_api_key,
            timeout=config.embedding_timeout_seconds,
            batch_size=config.embedding_batch_size,
        )
    elif kind == "subprocess":
        python = (
            Path(config.embedding_python)
            if config.embedding_python.strip()
            else default_embed_python(config.db_path.parent)
        )
        backend = EmbedProcessClient(
            python=python,
            server=_SERVER_SCRIPT,
            model=config.embedding_model,
            dims=config.embedding_dims,
            timeout=config.embedding_timeout_seconds,
            idle_seconds=config.embedding_idle_seconds,
            batch_size=config.embedding_batch_size,
        )
    else:
        raise EmbeddingError(f"未知的嵌入后端档位：{kind!r}")

    logger.info("嵌入后端就绪：%s（dims=%d）", backend.name, backend.dims)
    return backend
