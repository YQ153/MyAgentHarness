"""联网检索与网页抓取工具的回归测试。

WHY 用替身而不是真实网络：本组用例要断言的是「注册条件、错误映射、逐跳校验、
截断口径」——这些都不依赖对端行为，而一旦依赖真实站点，测试就会随对方改版、
限流、超时而随机失败。真实抓取另有一道真机验收（见计划 T14）。

WHY 地址一律用字面量公网 IP：字面量 IP 不触发 DNS 解析，测试因此不需要网络，
也不会因为本机 DNS 或 hosts 文件而在别人机器上变成另一种结果。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from langchain_core.tools import BaseTool

from agent.tools import ToolRegistry
from config import AppConfig
from tests.conftest import make_config
from web_tools import (
    RedirectLimitExceeded,
    UnsupportedContentError,
    UpstreamServiceError,
    WebToolError,
    register_tools,
)

_PUBLIC = "http://93.184.216.34"


# ------------------------------------------------------------------ HTTP 替身


class _StubResponse:
    """httpx 响应的最小替身，覆盖工具真正读取的那几个口。"""

    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.charset_encoding: str | None = None
        self._body = body
        self._chunks = chunks
        self.consumed_chunks = 0

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self._body.decode("utf-8"))

    async def aiter_bytes(self):
        for chunk in self._chunks if self._chunks is not None else [self._body]:
            self.consumed_chunks += 1
            yield chunk


class _StubStream:
    def __init__(self, response: _StubResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _StubResponse:
        return self._response

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _StubClient:
    """按请求顺序返回预置响应的客户端替身，并记录每一次请求。

    记录的是**完整请求**（含 ``params`` / ``json``），而不只是地址：检索适配器
    一旦漏掉 ``format=json`` 或把关键词放错字段，真实实例会返回错误结果或直接
    403，而「地址对得上」的断言抓不到这类问题。
    """

    def __init__(self, *responses: _StubResponse) -> None:
        self._responses = list(responses)
        self.requested: list[str] = []
        self.requests: list[dict[str, Any]] = []

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def _next(self, url: str, **kwargs: Any) -> _StubResponse:
        self.requested.append(url)
        self.requests.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"请求次数超出预置响应：{url}")
        return self._responses.pop(0)

    def stream(self, method: str, url: str, **kwargs: Any) -> _StubStream:
        return _StubStream(self._next(url, **kwargs))

    async def get(self, url: str, **kwargs: Any) -> _StubResponse:
        return self._next(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> _StubResponse:
        return self._next(url, **kwargs)


class _StubHttpx:
    """``httpx`` 模块替身：只换掉 ``AsyncClient``，异常类型透传真实实现。"""

    HTTPError = httpx.HTTPError

    def __init__(self, client: _StubClient) -> None:
        # 公开属性：用例需要读它记录的请求地址与构造参数
        self.client = client
        self.client_kwargs: list[dict[str, Any]] = []

    def AsyncClient(self, **kwargs: Any) -> _StubClient:
        self.client_kwargs.append(kwargs)
        return self.client


def _patch_http(monkeypatch: pytest.MonkeyPatch, *responses: _StubResponse) -> _StubHttpx:
    stub = _StubHttpx(_StubClient(*responses))
    monkeypatch.setattr("web_tools.httpx", stub)
    return stub


# ------------------------------------------------------------------ 装配辅助


def _tools(config: AppConfig) -> dict[str, BaseTool]:
    registry = ToolRegistry()
    register_tools(registry, config)
    return {item.name: item for item in registry.tools()}


def _tool(config: AppConfig, name: str) -> BaseTool:
    return _tools(config)[name]


def _text_response(body: bytes, content_type: str = "text/plain") -> _StubResponse:
    return _StubResponse(body=body, headers={"content-type": content_type})


# ================================================================== 注册条件


def test_register_requires_config():
    with pytest.raises(ValueError, match="需要配置对象"):
        register_tools(ToolRegistry())


def test_registers_only_fetch_when_provider_not_chosen(tmp_path):
    """默认 provider=none：抓取可用（不需要密钥），检索不出现。"""
    assert sorted(_tools(make_config(tmp_path))) == ["web_fetch"]


def test_skips_search_when_key_missing(tmp_path, caplog):
    config = make_config(tmp_path, web_search_provider="tavily")

    with caplog.at_level(logging.INFO):
        names = _tools(config)

    assert sorted(names) == ["web_fetch"]
    # 少一个工具必须能说清是哪个配置没填，否则运维只能翻源码猜
    assert "缺少 WEB_SEARCH_API_KEY" in caplog.text


def test_skips_search_when_provider_none(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        _tools(make_config(tmp_path))

    assert "未选择 provider" in caplog.text


def test_warns_when_key_present_but_provider_unset(tmp_path, caplog):
    """「配了密钥却没选 provider」是一次误配置，必须告警而不是静默缺席。

    WHY：这是实际踩过的坑——密钥就位、检索工具却没出现在清单里，而当时只有一条
    INFO，看不出到底缺什么。provider 决定调用哪家服务，不能因密钥存在就替用户选，
    所以只能把这条线索喊出来。
    """
    config = make_config(tmp_path, web_search_api_key="tvly-x")

    with caplog.at_level(logging.WARNING):
        names = _tools(config)

    assert sorted(names) == ["web_fetch"]
    assert "WEB_SEARCH_PROVIDER=none" in caplog.text


def test_no_warning_when_search_intentionally_off(tmp_path, caplog):
    """确实不打算开联网（无密钥 + provider=none）不该被喊成误配置。"""
    with caplog.at_level(logging.WARNING):
        _tools(make_config(tmp_path))

    assert "WEB_SEARCH_API_KEY 已配置" not in caplog.text


def test_registers_search_when_key_present(tmp_path):
    config = make_config(tmp_path, web_search_provider="tavily", web_search_api_key="tvly-x")

    assert sorted(_tools(config)) == ["web_fetch", "web_search"]


def test_registers_searxng_by_address_without_key(tmp_path):
    """自建 SearXNG 与 ollama 同口径：显式给地址即可用，不需要密钥。"""
    config = make_config(
        tmp_path, web_search_provider="searxng", web_search_base_url="http://127.0.0.1:8888"
    )

    assert sorted(_tools(config)) == ["web_fetch", "web_search"]


def test_skips_searxng_when_address_missing(tmp_path, caplog):
    config = make_config(tmp_path, web_search_provider="searxng")

    with caplog.at_level(logging.INFO):
        names = _tools(config)

    assert sorted(names) == ["web_fetch"]
    assert "缺少 WEB_SEARCH_BASE_URL" in caplog.text


# ================================================================== 抓取：正文


async def test_fetch_extracts_text_and_drops_scripts(tmp_path, monkeypatch):
    html = (
        "<html><head><style>p{color:red}</style><script>alert(1)</script></head>"
        "<body><h1>标题</h1><p>正文 &amp; 更多</p></body></html>"
    )
    _patch_http(monkeypatch, _text_response(html.encode("utf-8"), "text/html; charset=utf-8"))

    out = await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/page"})

    assert "标题" in out
    assert "正文 & 更多" in out
    # 脚本与样式既不是正文，又会白占上下文；先去标签再解实体，顺序反了会吃掉正文
    assert "alert(1)" not in out
    assert "color:red" not in out


async def test_fetch_keeps_plain_text_untouched(tmp_path, monkeypatch):
    _patch_http(monkeypatch, _text_response("a < b 且 c > d".encode("utf-8")))

    out = await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/t.txt"})

    # 非 HTML 不做标签剥离：否则 `a < b` 这类正常文本会被当成残缺标签删掉
    assert out == "a < b 且 c > d"


async def test_fetch_accepts_missing_content_type(tmp_path, monkeypatch):
    """不少站点根本不发 Content-Type；一律拒绝会让工具在最需要它的页面上失效。"""
    _patch_http(monkeypatch, _StubResponse(body=b"plain body"))

    out = await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/no-type"})

    assert out == "plain body"


async def test_fetch_survives_unknown_charset(tmp_path, monkeypatch):
    """服务端可能声明一个不存在的编码名；退回 UTF-8 总比整次抓取失败好。"""
    response = _StubResponse(body="中文".encode(), headers={"content-type": "text/plain"})
    response.charset_encoding = "x-not-a-real-charset"
    _patch_http(monkeypatch, response)

    out = await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/x"})

    assert out == "中文"


async def test_fetch_maps_timeout_and_passes_configured_timeout(tmp_path, monkeypatch):
    config = make_config(tmp_path, web_fetch_timeout_seconds=7.5)
    stub = _patch_http(monkeypatch)
    monkeypatch.setattr(
        stub.client, "stream", lambda *args, **kwargs: _raise(httpx.ReadTimeout("读取超时"))
    )

    with pytest.raises(UpstreamServiceError, match="抓取请求失败"):
        await _tool(config, "web_fetch").ainvoke({"url": f"{_PUBLIC}/slow"})

    # 超时必须按配置传入而不是写死：不同部署的网络与目标站点差异很大
    assert stub.client_kwargs[-1]["timeout"] == 7.5


async def test_fetch_rejects_empty_url(tmp_path, monkeypatch):
    _patch_http(monkeypatch)

    with pytest.raises(WebToolError, match="URL 不能为空"):
        await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": "   "})


# ================================================================== 抓取：错误映射


async def test_fetch_reports_upstream_error_status(tmp_path, monkeypatch):
    _patch_http(monkeypatch, _StubResponse(status_code=503, headers={"content-type": "text/plain"}))

    with pytest.raises(UpstreamServiceError, match="HTTP 503"):
        await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/down"})


async def test_fetch_rejects_binary_content(tmp_path, monkeypatch):
    _patch_http(monkeypatch, _text_response(b"\x89PNG", "image/png"))

    with pytest.raises(UnsupportedContentError, match="不是文本内容"):
        await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/a.png"})


def _raise(exc: Exception) -> Any:
    """构造一个「一进入上下文就抛异常」的对象，供传输层故障用例使用。"""

    class _Raiser:
        async def __aenter__(self) -> Any:
            raise exc

        async def __aexit__(self, *args: object) -> bool:
            return False

    return _Raiser()


async def test_fetch_maps_transport_error(tmp_path, monkeypatch):
    """传输层故障（连接被拒、DNS 失败、超时）必须收敛成可辨别的工具错误。"""
    stub = _patch_http(monkeypatch)
    monkeypatch.setattr(
        stub.client, "stream", lambda *args, **kwargs: _raise(httpx.ConnectError("连接被拒绝"))
    )

    with pytest.raises(UpstreamServiceError, match="抓取请求失败"):
        await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/x"})


# ================================================================== 抓取：SSRF


async def test_fetch_rejects_internal_address_without_request(tmp_path, monkeypatch):
    """红线：模型给出的内网地址必须在**发出请求之前**就被拒绝。"""
    stub = _patch_http(monkeypatch, _text_response(b"should-not-be-read"))

    with pytest.raises(WebToolError, match="出站安全策略"):
        await _tool(make_config(tmp_path), "web_fetch").ainvoke(
            {"url": "http://169.254.169.254/latest/meta-data/"}
        )

    # 一次请求都不该发出：拦住的是「让服务替攻击者访问」，不是在响应里筛一遍
    assert stub.client.requested == []


async def test_fetch_rejects_redirect_pointing_to_internal_address(tmp_path, monkeypatch):
    """逐跳校验的意义所在：首跳是公网地址，302 却指向内网。

    把重定向交给 httpx 的 follow_redirects 就会漏掉这一跳——这是 SSRF 最常被
    绕过的地方。"""
    stub = _patch_http(
        monkeypatch,
        _StubResponse(status_code=302, headers={"location": "http://127.0.0.1:8080/admin"}),
    )

    with pytest.raises(WebToolError, match="出站安全策略"):
        await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/redirect"})

    assert stub.client.requested == [f"{_PUBLIC}/redirect"]


async def test_fetch_rejects_non_http_redirect_target(tmp_path, monkeypatch):
    stub = _patch_http(
        monkeypatch,
        _StubResponse(status_code=302, headers={"location": "file:///etc/passwd"}),
    )

    with pytest.raises(WebToolError, match="http/https"):
        await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/redirect"})

    assert len(stub.client.requested) == 1


async def test_fetch_requires_location_header_on_redirect(tmp_path, monkeypatch):
    _patch_http(monkeypatch, _StubResponse(status_code=302))

    with pytest.raises(UpstreamServiceError, match="缺少 Location"):
        await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/redirect"})


async def test_fetch_follows_redirect_and_reports_final_url(tmp_path, monkeypatch):
    stub = _patch_http(
        monkeypatch,
        _StubResponse(status_code=302, headers={"location": f"{_PUBLIC}/final"}),
        _text_response(b"hello"),
    )

    out = await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/start"})

    assert out.startswith(f"（经重定向到达 {_PUBLIC}/final）")
    assert "hello" in out
    assert stub.client.requested == [f"{_PUBLIC}/start", f"{_PUBLIC}/final"]


async def test_fetch_resolves_relative_redirect(tmp_path, monkeypatch):
    stub = _patch_http(
        monkeypatch,
        _StubResponse(status_code=301, headers={"location": "/moved"}),
        _text_response(b"ok"),
    )

    await _tool(make_config(tmp_path), "web_fetch").ainvoke({"url": f"{_PUBLIC}/start"})

    # 相对 Location 必须相对**当前**地址解析，否则第二跳会打到别的站点
    assert stub.client.requested == [f"{_PUBLIC}/start", f"{_PUBLIC}/moved"]


async def test_fetch_stops_after_redirect_limit(tmp_path, monkeypatch):
    config = make_config(tmp_path, web_fetch_max_redirects=2)
    responses = [_StubResponse(status_code=302, headers={"location": f"{_PUBLIC}/hop"})] * 3
    stub = _patch_http(monkeypatch, *responses)

    with pytest.raises(RedirectLimitExceeded, match="重定向超过 2 次"):
        await _tool(config, "web_fetch").ainvoke({"url": f"{_PUBLIC}/start"})

    assert len(stub.client.requested) == 3


# ================================================================== 抓取：截断口径


async def test_fetch_marks_truncation_explicitly(tmp_path, monkeypatch):
    """延续内置工具的截断口径：模型必须能看出「内容到此为止」还是「被截掉了」。"""
    config = make_config(tmp_path, web_fetch_max_chars=500)
    _patch_http(monkeypatch, _text_response(b"x" * 5000))

    out = await _tool(config, "web_fetch").ainvoke({"url": f"{_PUBLIC}/big"})

    assert out.endswith("... Output truncated at 500 chars.")


async def test_fetch_caps_bytes_read_before_decoding(tmp_path, monkeypatch):
    """只限字符数挡不住「响应体巨大但正文很少」：内存是在读完那一刻被占满的。"""
    config = make_config(tmp_path, web_fetch_max_chars=500)
    response = _StubResponse(chunks=[b"a" * 1000] * 10, headers={"content-type": "text/plain"})
    _patch_http(monkeypatch, response)

    out = await _tool(config, "web_fetch").ainvoke({"url": f"{_PUBLIC}/huge"})

    assert out.endswith("... Output truncated at 500 chars.")
    # 500 字符 × 4 字节 = 2000 字节上限 → 读到第 2 块就应当停下
    assert response.consumed_chunks == 2


async def test_fetch_uses_configured_user_agent(tmp_path, monkeypatch):
    config = make_config(tmp_path, web_user_agent="Custom/1.0")
    stub = _patch_http(monkeypatch, _text_response(b"ok"))

    await _tool(config, "web_fetch").ainvoke({"url": f"{_PUBLIC}/"})

    assert stub.client_kwargs[-1]["headers"]["User-Agent"] == "Custom/1.0"


# ================================================================== 检索


def _search_config(tmp_path, **overrides: Any) -> AppConfig:
    params: dict[str, Any] = {
        "web_search_provider": "tavily",
        "web_search_api_key": "tvly-test",
    }
    params.update(overrides)
    return make_config(tmp_path, **params)


def _tavily_body(results: list[dict[str, Any]]) -> bytes:
    return json.dumps({"results": results}).encode("utf-8")


async def test_search_renders_title_url_and_snippet(tmp_path, monkeypatch):
    _patch_http(
        monkeypatch,
        _StubResponse(
            headers={"content-type": "application/json"},
            body=_tavily_body(
                [
                    {"title": "甲", "url": "https://a.example/1", "content": "摘要一"},
                    {"title": "乙", "url": "https://b.example/2", "snippet": "摘要二"},
                ]
            ),
        ),
    )

    out = await _tool(_search_config(tmp_path), "web_search").ainvoke({"query": "测试"})

    assert "命中 2 条" in out
    assert "甲" in out and "https://a.example/1" in out and "摘要一" in out
    # 摘要字段名各 provider 不统一，snippet 命中时必须也能取到
    assert "摘要二" in out


async def test_search_reports_empty_results_without_raising(tmp_path, monkeypatch):
    """空结果不是错误：没搜到与上游挂了要采取的动作完全不同。"""
    _patch_http(
        monkeypatch, _StubResponse(headers={"content-type": "application/json"}, body=_tavily_body([]))
    )

    out = await _tool(_search_config(tmp_path), "web_search").ainvoke({"query": "冷门词"})

    assert "未检索到" in out


async def test_search_drops_results_without_url(tmp_path, monkeypatch):
    """没有链接的结果对「再去抓正文」这条链路毫无用处，留着只会误导模型。"""
    _patch_http(
        monkeypatch,
        _StubResponse(
            headers={"content-type": "application/json"},
            body=_tavily_body([{"title": "无链接", "content": "x"}, {"title": "有链接", "url": "https://c.example/3"}]),
        ),
    )

    out = await _tool(_search_config(tmp_path), "web_search").ainvoke({"query": "q"})

    assert "命中 1 条" in out
    assert "无链接" not in out


async def test_search_maps_upstream_error(tmp_path, monkeypatch):
    _patch_http(monkeypatch, _StubResponse(status_code=500, body=b"boom"))

    with pytest.raises(UpstreamServiceError, match="HTTP 500"):
        await _tool(_search_config(tmp_path), "web_search").ainvoke({"query": "q"})


async def test_search_rejects_unexpected_payload_shape(tmp_path, monkeypatch):
    _patch_http(
        monkeypatch,
        _StubResponse(headers={"content-type": "application/json"}, body=b'{"hits": []}'),
    )

    with pytest.raises(UpstreamServiceError, match="没有 results"):
        await _tool(_search_config(tmp_path), "web_search").ainvoke({"query": "q"})


async def test_search_rejects_non_object_payload(tmp_path, monkeypatch):
    """顶层是数组而不是对象时，必须报「结构非预期」而不是在解析里炸掉。"""
    _patch_http(
        monkeypatch,
        _StubResponse(headers={"content-type": "application/json"}, body=b"[]"),
    )

    with pytest.raises(UpstreamServiceError, match="不是 JSON 对象"):
        await _tool(_search_config(tmp_path), "web_search").ainvoke({"query": "q"})


async def test_search_skips_non_object_result_items(tmp_path, monkeypatch):
    _patch_http(
        monkeypatch,
        _StubResponse(
            headers={"content-type": "application/json"},
            body=_tavily_body(["整条不是对象", 42]),
        ),
    )

    out = await _tool(_search_config(tmp_path), "web_search").ainvoke({"query": "q"})

    assert "未检索到" in out


async def test_search_maps_transport_error(tmp_path, monkeypatch):
    stub = _patch_http(monkeypatch)

    async def _boom(url: str, **kwargs: Any) -> _StubResponse:
        raise httpx.ConnectError("连接被拒绝")

    monkeypatch.setattr(stub.client, "post", _boom)

    with pytest.raises(UpstreamServiceError, match="检索请求失败"):
        await _tool(_search_config(tmp_path), "web_search").ainvoke({"query": "q"})


async def test_search_rejects_empty_query(tmp_path, monkeypatch):
    _patch_http(monkeypatch)

    with pytest.raises(WebToolError, match="关键词不能为空"):
        await _tool(_search_config(tmp_path), "web_search").ainvoke({"query": "  "})


async def test_search_limits_result_count(tmp_path, monkeypatch):
    config = _search_config(tmp_path, web_search_max_results=2)
    _patch_http(
        monkeypatch,
        _StubResponse(
            headers={"content-type": "application/json"},
            body=_tavily_body(
                [{"title": f"t{i}", "url": f"https://e.example/{i}"} for i in range(5)]
            ),
        ),
    )

    out = await _tool(config, "web_search").ainvoke({"query": "q"})

    assert "命中 2 条" in out


async def test_searxng_uses_configured_loopback_address(tmp_path, monkeypatch):
    """自建 SearXNG 常跑在 127.0.0.1：配置地址属受信输入，不该被 SSRF 判定挡住。

    这条与 web_fetch 的内网拒绝互为对照——判据是「谁选了这个地址」，
    而不是「地址长什么样」。"""
    config = make_config(
        tmp_path, web_search_provider="searxng", web_search_base_url="http://127.0.0.1:8888"
    )
    stub = _patch_http(
        monkeypatch, _StubResponse(headers={"content-type": "application/json"}, body=_tavily_body([]))
    )

    await _tool(config, "web_search").ainvoke({"query": "q"})

    assert stub.client.requested == ["http://127.0.0.1:8888/search"]


async def test_tavily_posts_key_query_and_limit(tmp_path, monkeypatch):
    """断言请求体本身，而不只是地址。

    WHY：密钥放错字段、上限没传、关键词没去空白，都不会让「地址正确」的断言转红，
    但在真实 Tavily 上分别是 401、结果条数失控、查询词带噪音。这些是替身唯一
    能提前替真实服务挡住的一类错误。
    """
    config = make_config(
        tmp_path,
        web_search_provider="tavily",
        web_search_api_key="tvly-test",
        web_search_max_results=3,
    )
    stub = _patch_http(
        monkeypatch, _StubResponse(headers={"content-type": "application/json"}, body=_tavily_body([]))
    )

    await _tool(config, "web_search").ainvoke({"query": "  python asyncio  "})

    request = stub.client.requests[0]
    assert request["url"] == "https://api.tavily.com/search"
    assert request["json"] == {
        "api_key": "tvly-test",
        "query": "python asyncio",
        "max_results": 3,
        "search_depth": "basic",
    }


async def test_searxng_sends_query_and_json_format(tmp_path, monkeypatch):
    """断言 SearXNG 请求带 ``q`` 与 ``format=json``。

    WHY 必须钉住 ``format``：SearXNG 默认只输出 HTML，缺了它真实实例会返回 HTML
    （或直接 403），表现为「实例可达、适配器却解析失败」——最容易与被误判成实例
    配置问题的一类故障。JSON 输出还需在实例侧显式开启，两处缺一不可。
    """
    config = make_config(
        tmp_path, web_search_provider="searxng", web_search_base_url="http://127.0.0.1:8888"
    )
    stub = _patch_http(
        monkeypatch, _StubResponse(headers={"content-type": "application/json"}, body=_tavily_body([]))
    )

    await _tool(config, "web_search").ainvoke({"query": "asyncio"})

    request = stub.client.requests[0]
    assert request["url"] == "http://127.0.0.1:8888/search"
    assert request["params"] == {"q": "asyncio", "format": "json"}


async def test_searxng_rejects_address_without_scheme(tmp_path, monkeypatch):
    config = make_config(
        tmp_path, web_search_provider="searxng", web_search_base_url="127.0.0.1:8888"
    )
    _patch_http(monkeypatch, _StubResponse(headers={"content-type": "application/json"}, body=b"{}"))

    with pytest.raises(WebToolError, match="必须是 http/https"):
        await _tool(config, "web_search").ainvoke({"query": "q"})
