"""嵌入后端的装配与响应解析。

覆盖三类容易静默出错的地方：
1. **请求本身**（地址、请求体、鉴权头）——只断言「地址对得上」抓不到字段写错；
2. **响应的顺序与形状**——错位或维度不符若被放过，写进索引的就是语义错误的向量；
3. **装配口径**——默认档位必须不装配任何后端。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from config import AppConfig
from llm.embed_process import EmbedProcessClient, default_embed_python
from llm.embeddings import (
    EmbeddingBackend,
    EmbeddingError,
    OpenAICompatEmbeddings,
    build_embeddings,
)
from tests.conftest import make_config


class _StubResponse:
    """最小响应替身：只提供被测代码用到的三个成员。"""

    def __init__(self, payload: Any = None, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    def json(self) -> Any:
        """返回预置载荷；载荷为 ``None`` 时模拟「响应不是 JSON」。"""
        if self._payload is None:
            raise ValueError("响应不是 JSON")
        return self._payload


class _StubClient:
    """记录请求、按序返回预置响应的客户端替身。"""

    def __init__(self, *responses: _StubResponse) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def post(self, url: str, **kwargs: Any) -> _StubResponse:
        self.requests.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"请求次数超出预置响应：{url}")
        return self._responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def _items(*vectors: list[float]) -> dict[str, Any]:
    """按 OpenAI 兼容格式拼一个响应体。"""
    return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}


def _backend(client: _StubClient, **overrides: Any) -> OpenAICompatEmbeddings:
    """构造一个注入了替身客户端的后端。"""
    params: dict[str, Any] = {
        "base_url": "http://embed.local:8080",
        "model": "bge-small-zh",
        "dims": 3,
        "client": client,
    }
    params.update(overrides)
    return OpenAICompatEmbeddings(**params)


# --------------------------------------------------------------- 请求构造


async def test_embed_posts_model_and_input_to_v1_embeddings() -> None:
    """断言请求体本身，而不只是地址。

    WHY：模型名放错字段、把 ``input`` 写成 ``texts``、漏掉 ``/v1`` 前缀，都不会让
    「地址正确」的断言转红，但在真实服务上分别是「用了错的模型」、400 与 404。
    """
    client = _StubClient(_StubResponse(_items([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])))
    backend = _backend(client)

    await backend.embed(["甲", "乙"])

    request = client.requests[0]
    assert request["url"] == "http://embed.local:8080/v1/embeddings"
    assert request["json"] == {"model": "bge-small-zh", "input": ["甲", "乙"]}


async def test_base_url_trailing_slash_is_normalized() -> None:
    """结尾斜杠不应拼出 ``//v1/embeddings``。"""
    client = _StubClient(_StubResponse(_items([1.0, 2.0, 3.0])))
    backend = _backend(client, base_url="http://embed.local:8080/")

    await backend.embed(["甲"])

    assert client.requests[0]["url"] == "http://embed.local:8080/v1/embeddings"


async def test_bearer_header_only_when_key_present() -> None:
    """无密钥时不发 ``Authorization`` 头。

    WHY：本地 TEI / Ollama 不需要密钥，发一个空的 Bearer 会让某些实现直接 401。
    """
    without = _StubClient(_StubResponse(_items([1.0, 2.0, 3.0])))
    await _backend(without).embed(["甲"])
    assert without.requests[0]["headers"] == {}

    with_key = _StubClient(_StubResponse(_items([1.0, 2.0, 3.0])))
    await _backend(with_key, api_key="sk-test").embed(["甲"])
    assert with_key.requests[0]["headers"] == {"Authorization": "Bearer sk-test"}


async def test_embed_splits_into_batches_preserving_order() -> None:
    """5 条文本按批大小 2 拆成 3 次请求，且结果顺序与入参一致。"""
    client = _StubClient(
        _StubResponse(_items([0.0, 0.0, 0.0], [1.0, 1.0, 1.0])),
        _StubResponse(_items([2.0, 2.0, 2.0], [3.0, 3.0, 3.0])),
        _StubResponse(_items([4.0, 4.0, 4.0])),
    )
    backend = _backend(client, batch_size=2)

    vectors = await backend.embed(["a", "b", "c", "d", "e"])

    assert len(client.requests) == 3
    assert [request["json"]["input"] for request in client.requests] == [
        ["a", "b"],
        ["c", "d"],
        ["e"],
    ]
    assert [vector[0] for vector in vectors] == [0.0, 1.0, 2.0, 3.0, 4.0]


async def test_empty_input_makes_no_request() -> None:
    """空入参直接返回，不发请求。"""
    client = _StubClient()
    backend = _backend(client)

    assert await backend.embed([]) == []
    assert client.requests == []


# --------------------------------------------------------------- 响应解析


async def test_vectors_are_ordered_by_index_not_arrival() -> None:
    """按 ``index`` 排序，而不是信任返回顺序。

    WHY：错位不会报错，只会把「A 的向量」配给「B 的文本」，检索结果长期歪掉。
    """
    payload = {
        "data": [
            {"index": 1, "embedding": [1.0, 1.0, 1.0]},
            {"index": 0, "embedding": [0.0, 0.0, 0.0]},
        ]
    }
    client = _StubClient(_StubResponse(payload))

    vectors = await _backend(client).embed(["第一", "第二"])

    assert vectors == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]


async def test_count_mismatch_is_rejected() -> None:
    """返回条数与请求条数不符时必须报错，而不是少写几条进索引。"""
    client = _StubClient(_StubResponse(_items([1.0, 2.0, 3.0])))

    with pytest.raises(EmbeddingError, match="嵌入条数不符"):
        await _backend(client).embed(["甲", "乙"])


async def test_dim_mismatch_points_at_the_config_item() -> None:
    """维度不符的报错要指向配置项与「重建索引」，而不是只说长度不对。"""
    client = _StubClient(_StubResponse(_items([1.0, 2.0])))

    with pytest.raises(EmbeddingError, match="EMBEDDING_DIMS"):
        await _backend(client).embed(["甲"])


async def test_missing_index_is_rejected() -> None:
    """缺 ``index`` 时不能靠列表顺序兜底——那正是错位的来源。"""
    client = _StubClient(_StubResponse({"data": [{"embedding": [1.0, 2.0, 3.0]}]}))

    with pytest.raises(EmbeddingError, match="index"):
        await _backend(client).embed(["甲"])


async def test_missing_data_array_is_rejected() -> None:
    """响应体里没有 ``data`` 时要给出可读原因。"""
    client = _StubClient(_StubResponse({"error": "unauthorized"}))

    with pytest.raises(EmbeddingError, match="data"):
        await _backend(client).embed(["甲"])


async def test_http_error_status_is_reported_with_body() -> None:
    """非 2xx 要把状态码与响应体一起带出来。"""
    client = _StubClient(_StubResponse({"detail": "model not loaded"}, status_code=503))

    with pytest.raises(EmbeddingError, match="503"):
        await _backend(client).embed(["甲"])


async def test_non_json_response_is_reported() -> None:
    """返回 HTML 错误页时不能抛出一条 JSON 解析栈。"""
    client = _StubClient(_StubResponse(None))

    with pytest.raises(EmbeddingError, match="不是 JSON"):
        await _backend(client).embed(["甲"])


async def test_transport_error_is_wrapped() -> None:
    """网络层失败要收敛为 ``EmbeddingError``，便于调用方降级。"""
    import httpx

    class _Failing:
        async def post(self, url: str, **kwargs: Any) -> Any:
            raise httpx.ConnectError("connection refused")

        async def aclose(self) -> None:
            return None

    with pytest.raises(EmbeddingError, match="请求失败"):
        await _backend(_Failing()).embed(["甲"])  # type: ignore[arg-type]


# --------------------------------------------------------------- 生命周期


async def test_aclose_closes_only_self_owned_client() -> None:
    """注入的客户端归调用方管理，后端不能替它关掉。

    WHY：测试夹具与上层装配可能共享一个客户端；被下游提前关闭后，下一次请求会以
    「连接已关闭」失败，而那个错与真正的问题毫无关系。
    """
    injected = _StubClient(_StubResponse(_items([1.0, 2.0, 3.0])))
    await _backend(injected).aclose()
    assert injected.closed is False

    owned = _StubClient(_StubResponse(_items([1.0, 2.0, 3.0])))
    backend = OpenAICompatEmbeddings(
        base_url="http://embed.local:8080", model="m", dims=3, client=owned
    )
    backend._owns_client = True  # noqa: SLF001 - 直接构造「自建客户端」的状态
    await backend.aclose()
    assert owned.closed is True


def test_construction_rejects_empty_base_url_and_bad_dims() -> None:
    """缺地址与非正维度在构造期就要拦下。"""
    with pytest.raises(EmbeddingError, match="base_url"):
        OpenAICompatEmbeddings(base_url="  ", model="m", dims=3)
    with pytest.raises(EmbeddingError, match="dims"):
        OpenAICompatEmbeddings(base_url="http://x", model="m", dims=0)


# --------------------------------------------------------------- 装配


def test_build_embeddings_returns_none_for_disabled_backend(tmp_path) -> None:
    """``none`` 档位返回 ``None``——它是设计上的降级，不是故障。"""
    config = make_config(tmp_path)

    assert config.embedding_backend == "none"
    assert build_embeddings(config) is None


def test_build_embeddings_wires_openai_compat_from_config(tmp_path) -> None:
    """``openai-compat`` 档位的每个字段都来自配置。"""
    config = make_config(
        tmp_path,
        embedding_backend="openai-compat",
        embedding_base_url="http://embed:80",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_dims=512,
        embedding_api_key="sk-test",
        embedding_batch_size=8,
    )

    backend = build_embeddings(config)

    assert isinstance(backend, OpenAICompatEmbeddings)
    assert backend.dims == 512
    assert backend.name == "openai-compat:BAAI/bge-small-zh-v1.5"


def test_build_embeddings_subprocess_uses_convention_path(tmp_path) -> None:
    """``subprocess`` 档位默认把模型环境放在数据目录下（与数据同卷）。"""
    config = make_config(tmp_path, embedding_backend="subprocess", embedding_dims=512)

    backend = build_embeddings(config)

    assert isinstance(backend, EmbedProcessClient)
    assert backend.python == default_embed_python(tmp_path)
    assert backend.dims == 512


def test_build_embeddings_subprocess_honors_explicit_python(tmp_path) -> None:
    """显式 ``EMBEDDING_PYTHON`` 优先于约定路径。"""
    custom = tmp_path / "elsewhere" / "python"
    config = make_config(
        tmp_path, embedding_backend="subprocess", embedding_python=str(custom)
    )

    backend = build_embeddings(config)

    assert isinstance(backend, EmbedProcessClient)
    assert backend.python == custom


def test_backends_satisfy_the_protocol(tmp_path) -> None:
    """两个实现都要满足协议，否则「可插拔」只是文档上的一句话。"""
    openai_backend = OpenAICompatEmbeddings(base_url="http://x", model="m", dims=3)
    subprocess_backend = EmbedProcessClient(
        python=tmp_path / "python", server=tmp_path / "s.py", model="m", dims=3
    )

    assert isinstance(openai_backend, EmbeddingBackend)
    assert isinstance(subprocess_backend, EmbeddingBackend)


# --------------------------------------------------------------- 配置校验


def test_openai_compat_requires_base_url_at_load_time(tmp_path) -> None:
    """缺地址要在**加载期**拦下，而不是等首次嵌入时在栈顶看到网络错误。"""
    with pytest.raises(ValidationError, match="EMBEDDING_BASE_URL"):
        make_config(tmp_path, embedding_backend="openai-compat", embedding_base_url="")


def test_embedding_backend_normalizes_case(tmp_path) -> None:
    """``OPENAI-COMPAT`` / `` openai-compat `` 都应被归一。"""
    config = make_config(
        tmp_path, embedding_backend="  OPENAI-COMPAT  ", embedding_base_url="http://embed:80"
    )

    assert config.embedding_backend == "openai-compat"


def test_embedding_dims_has_lower_bound(tmp_path) -> None:
    """维度为 0 会让向量表建不出来，必须在配置层拦下。"""
    with pytest.raises(ValidationError):
        make_config(tmp_path, embedding_dims=0)


def test_embedding_api_key_is_not_in_repr(tmp_path) -> None:
    """密钥不得出现在 repr 里——配置对象会进日志与断言输出。"""
    config: AppConfig = make_config(tmp_path, embedding_api_key="sk-secret-value")

    assert "sk-secret-value" not in repr(config)
