"""联网检索与网页抓取：内置的「自定义工具模块」。

WHY 做成自定义工具模块而不是内核能力：新增工具不应修改内核，这正是 T8 留下的
扩展点的用途。本模块通过 ``register_tools(registry, config)`` 取配置——``.env``
里的值只进配置对象、不进 ``os.environ``，模块自己去读环境变量是读不到密钥的，
这就是扩展点必须能把配置传进来的原因。

WHY 默认不加载：联网会把用户的查询词与待访问地址发给第三方。「是否启用」由运营方
在 ``CUSTOM_TOOL_MODULES`` 里显式列入决定（``CUSTOM_TOOL_MODULES=web_tools``），
而不是默认打开——替用户决定「可以把查询发给谁」不是默认该做的事。

WHY 两个工具分开注册：检索需要密钥（缺了就不注册，清单里不出现一个「点了才报
缺少密钥」的条目）；抓取不需要密钥，只受出站安全策略约束。把两者绑在一个开关上
会让「只想抓公网页面」的部署被迫先申请一个检索服务。
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import urljoin

import httpx
from langchain_core.tools import BaseTool, tool

from agent.tools import ToolRegistry, ToolSource
from text_utils import truncate_with_notice
from web_safety import OutboundAddressRejected, validate_outbound_url

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = "MyAgentHarness/0.1 (+https://github.com/YQ153/MyAgentHarness)"
"""出站请求的默认 User-Agent。

WHY 不自称浏览器：伪装 UA 会让对方按浏览器行为对待（返回重前端页、放宽限流），
而本工具只取正文；如实标识也方便对端在日志里认出流量来源。
"""

_TAVILY_BASE_URL = "https://api.tavily.com"
_BYTES_PER_CHAR = 4
"""由字符上限换算字节上限的系数。

WHY 需要字节上限：只限字符数挡不住「响应体巨大但正文很少」的页面（例如几十 MB
的内联脚本），而内存是在读完整个响应体那一刻就被占满的。UTF-8 单字符最多 4 字节，
用它换算得到的是一个宽松但确定的上界，不需要再引入一个配置项。
"""

_SNIPPET_MAX_CHARS = 500
"""单条检索结果摘要写入工具输出的字符上限；检索结果会整体进上下文。"""

_SCRIPT_OR_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_SPACE_RE = re.compile(r"[ \t\f\v]+")
_MANY_NEWLINES_RE = re.compile(r"\n\s*\n\s*\n+")


class WebToolError(RuntimeError):
    """联网工具的可预期失败。

    WHY 让这些失败以异常形式浮出而不是返回一句错误文本：审计按工具调用的
    结果状态落库，返回文本会被记成 ``success``——一次「上游 500」在审计里
    看起来像一次成功调用，等于把观测面弄脏。
    """


class UpstreamServiceError(WebToolError):
    """上游服务返回了错误状态或不可用的响应。"""


class RedirectLimitExceeded(WebToolError):
    """重定向次数超出上限。"""


class UnsupportedContentError(WebToolError):
    """响应不是可提取正文的文本类型。"""


class SearchResult(NamedTuple):
    """一条检索结果。"""

    title: str
    url: str
    snippet: str


SearchHandler = Callable[[httpx.AsyncClient, "AppConfig", str, int], Awaitable[list[SearchResult]]]
"""检索 provider 的实现签名：``(client, config, query, limit) -> 结果列表``。

WHY 单独起一个别名：``_SEARCH_PROVIDERS`` 表与适配器函数是成对演进的，签名写在一处
才能让「新 provider 忘记对齐参数」在类型检查阶段就暴露。
"""


# ------------------------------------------------------------------ 错误与结果渲染


def _truncate(text: str, limit: int) -> str:
    """按字符上限截断并显式标注。

    WHY 把判断交给 ``text_utils.truncate_with_notice``：截断与否、切到哪里是
    与标题截断、检索片段截断共用的同一个决策，分开写迟早出现「这里标了、那里没标」。
    留在本模块的只有措辞——这段文本会被模型当输入读，所以要英文，并带上具体字符数，
    让模型知道自己看到的是残缺内容而不是全部。
    """
    return truncate_with_notice(text, limit, f"\n\n... Output truncated at {limit} chars.")


def _render_results(query: str, results: list[SearchResult]) -> str:
    """把检索结果渲染成模型易读的文本。

    WHY 用文本而不是 JSON：模型对「编号 + 标题 + 链接 + 摘要」这种版式最不容易
    误读，而 JSON 的括号与转义会白占一批 token。
    """
    if not results:
        # WHY 空结果不当异常：检索成功但没命中是正常结果，把它记成 error 会让
        # 审计里「检索失败」与「没搜到」混成一类，而二者要采取的动作不同。
        return f"未检索到与「{query}」相关的结果。可尝试更宽泛的关键词，或换用其它资料源。"
    lines = [f"检索「{query}」命中 {len(results)} 条："]
    for index, item in enumerate(results, start=1):
        lines.append(
            f"{index}. {item.title}\n"
            f"   链接：{item.url}\n"
            f"   摘要：{_truncate(item.snippet, _SNIPPET_MAX_CHARS)}"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ 检索 provider


def _search_unavailable_reason(config: AppConfig) -> str | None:
    """返回「检索工具为何不注册」的原因；可用时返回 ``None``。

    WHY 与注册规则写在同一个函数里：拆成两个地方后，日志里说明的原因迟早会与
    实际判定条件分叉，而「配置齐全却没注册」是最难排查的一类问题。
    """
    provider = (config.web_search_provider or "none").strip().lower()
    if provider == "none":
        return "未选择 provider（WEB_SEARCH_PROVIDER=none）"
    if provider == "tavily" and not config.web_search_api_key.strip():
        return "缺少 WEB_SEARCH_API_KEY"
    if provider == "searxng" and not config.web_search_base_url.strip():
        return "缺少 WEB_SEARCH_BASE_URL（自建 SearXNG 必须显式提供地址）"
    return None


def _key_present_but_provider_unset(config: AppConfig) -> bool:
    """是否「配了检索密钥，却没选 provider」——一个会导致工具静默缺席的组合。

    WHY 判定与 ``_search_unavailable_reason`` 分开：后者回答「为什么没注册」，
    这里回答「这是不是一次误配置」。合并成一句会把「确实不想开联网」也喊成告警。
    """
    provider = (config.web_search_provider or "none").strip().lower()
    return provider == "none" and bool(config.web_search_api_key.strip())


def _search_base_url(config: AppConfig, default: str) -> str:
    """取检索服务地址：配置优先，其次 provider 官方地址。"""
    configured = (config.web_search_base_url or "").strip()
    return (configured or default).rstrip("/")


def _require_http_url(url: str, setting: str) -> str:
    """校验配置里的服务地址。

    WHY 只校验 scheme 而不做 SSRF 判定：配置地址由运营方填写，属于受信输入；
    自建 SearXNG 跑在 ``127.0.0.1`` 是正当用法，拿模型输入的尺子去量它会把正确
    的部署禁掉。这里挡的是「少写 scheme」这类笔误。
    """
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        raise WebToolError(f"{setting} 必须是 http/https 地址，实际：{url}")
    return url


def _parse_results(payload: Any) -> list[SearchResult]:
    """从 provider 响应里取出结果列表。

    WHY 容忍字段缺失而不是直接报错：上游对摘要字段的命名并不统一（``content`` /
    ``snippet`` / ``description``），缺一个字段就让整次检索失败，代价远大于收益。
    """
    if not isinstance(payload, dict):
        raise UpstreamServiceError("检索服务返回了非预期的响应结构（不是 JSON 对象）")
    raw_items = payload.get("results")
    if not isinstance(raw_items, list):
        raise UpstreamServiceError("检索服务响应里没有 results 列表")

    results: list[SearchResult] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            # 没有链接的结果对「再去抓正文」这条链路毫无用处，留着只会误导模型
            continue
        snippet = item.get("content") or item.get("snippet") or item.get("description") or ""
        results.append(
            SearchResult(
                title=str(item.get("title") or url).strip(),
                url=url,
                snippet=str(snippet).strip(),
            )
        )
    return results


async def _search_tavily(
    client: httpx.AsyncClient, config: AppConfig, query: str, limit: int
) -> list[SearchResult]:
    """Tavily：托管检索服务。

    说明：本适配器按 ``POST /search`` 且密钥置于请求体的既有口径实现，
    未在本机对真实服务做过端到端验证（仓库内无可用密钥）——本条已在计划中登记。
    """
    base = _require_http_url(_search_base_url(config, _TAVILY_BASE_URL), "WEB_SEARCH_BASE_URL")
    response = await client.post(
        f"{base}/search",
        json={
            "api_key": config.web_search_api_key.strip(),
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
        },
    )
    _raise_for_upstream(response, "Tavily")
    return _parse_results(response.json())


async def _search_searxng(
    client: httpx.AsyncClient, config: AppConfig, query: str, limit: int
) -> list[SearchResult]:
    """SearXNG：自建元搜索，不需要密钥，但必须在实例上开启 JSON 输出。"""
    # ``_require_http_url`` 除非地址以 http(s):// 开头否则必抛错，故返回值非空，
    # 无需再判一次「地址没配」——那是 ``_search_unavailable_reason`` 的职责。
    base = _require_http_url(_search_base_url(config, ""), "WEB_SEARCH_BASE_URL")
    response = await client.get(f"{base}/search", params={"q": query, "format": "json"})
    _raise_for_upstream(response, "SearXNG")
    return _parse_results(response.json())[:limit]


def _raise_for_upstream(response: httpx.Response, name: str) -> None:
    """把上游的非 2xx 状态映射成一条能指出原因的错误。"""
    if response.status_code >= 400:
        detail = response.text.strip()[:200]
        raise UpstreamServiceError(
            f"{name} 返回 HTTP {response.status_code}"
            + (f"：{detail}" if detail else "")
        )


_SEARCH_PROVIDERS = {
    "tavily": _search_tavily,
    "searxng": _search_searxng,
}
"""provider 名到实现的映射。

WHY 用表而不是 if-else：新增 provider 时只加一行，而 ``_search_unavailable_reason``
负责的「可用性判定」与这里必须成对演进——加表单忘了加判定，结果是「注册了却调不通」。
"""


# ------------------------------------------------------------------ 抓取


def _is_textual(content_type: str) -> bool:
    """判断响应类型是否可能含可提取的正文。

    WHY 缺 Content-Type 时按文本处理：不少站点根本不发这个头，一律拒绝会让工具
    在最需要它的页面上失效；而真实二进制类型（图片、压缩包）绝大多数会带上类型，
    白名单加一条「缺省放行」比黑名单更贴合实际。
    """
    if not content_type:
        return True
    main = content_type.split(";", 1)[0].strip().lower()
    if main.startswith("text/"):
        return True
    return main in {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/rss+xml",
        "application/atom+xml",
    } or main.endswith(("+json", "+xml"))


def _html_to_text(raw: str, *, is_html: bool) -> str:
    """把 HTML 压成纯文本。

    WHY 自己剥标签而不引第三方库：这里只需要「读到正文」，不需要完整的 DOM 语义，
    而规则总共三条（去 script/style、去标签、解实体），写成十行比读一遍 readability
    的实现更省事——它的价值在正文抽取的启发式，而那部分这里用不上。

    WHY 先去标签再解实体：顺序反了的话，正文里被转义的 ``&lt;div&gt;`` 会在解实体
    之后被当成真标签删掉——那是内容丢失，不是清洗。
    """
    text = raw
    if is_html:
        text = _SCRIPT_OR_STYLE_RE.sub(" ", text)
        text = _TAG_RE.sub(" ", text)
        text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _INLINE_SPACE_RE.sub(" ", text)
    text = _MANY_NEWLINES_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[str, bool]:
    """按字节上限读取响应体，返回 ``(文本, 是否被截断)``。"""
    buffer = bytearray()
    truncated = False
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        if len(buffer) >= max_bytes:
            del buffer[max_bytes:]
            truncated = True
            break

    encoding = response.charset_encoding or "utf-8"
    try:
        text = bytes(buffer).decode(encoding, errors="replace")
    except LookupError:
        # 服务端声明了未知编码名；退回 UTF-8 并替换非法字节，总比整次抓取失败好
        text = bytes(buffer).decode("utf-8", errors="replace")
    return text, truncated


async def _fetch_text(
    url: str,
    *,
    timeout: float,
    max_redirects: int,
    max_chars: int,
    user_agent: str,
) -> tuple[str, str, bool]:
    """抓取并提取正文，返回 ``(最终地址, 文本, 是否被截断)``。

    WHY 自己走重定向而不是交给 httpx 的 ``follow_redirects``：交给它意味着第二跳
    之后的目标不再经过出站校验——一次 302 就能把请求引到内网地址，这正是 SSRF
    最容易被绕过的地方。这里每跳都重新校验。

    Raises:
        OutboundAddressRejected: 目标被出站安全策略拒绝。
        RedirectLimitExceeded: 重定向次数超出上限。
        UpstreamServiceError: 目标返回 4xx/5xx。
        UnsupportedContentError: 响应不是文本类型。
    """
    max_bytes = max_chars * _BYTES_PER_CHAR
    current = url

    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": user_agent or _DEFAULT_USER_AGENT},
        follow_redirects=False,
    ) as client:
        for _ in range(max_redirects + 1):
            target = validate_outbound_url(current)
            async with client.stream("GET", target) as response:
                # WHY 按状态码区间判断而不是用 httpx 的 ``is_redirect``：后者要求
                # 响应里带 Location，于是「302 但缺 Location」会落进正常分支、
                # 被当成一次成功抓取并返回空正文——静默的错误结果比报错更糟。
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise UpstreamServiceError(f"重定向响应缺少 Location：{target}")
                    current = urljoin(target, location)
                    continue

                if response.status_code >= 400:
                    raise UpstreamServiceError(f"目标返回 HTTP {response.status_code}：{target}")

                content_type = response.headers.get("content-type", "")
                if not _is_textual(content_type):
                    raise UnsupportedContentError(
                        f"该地址不是文本内容（Content-Type: {content_type or '未知'}）：{target}"
                    )

                raw, hit_byte_cap = await _read_capped(response, max_bytes)
                is_html = "html" in content_type.lower()
                text = _html_to_text(raw, is_html=is_html)
                final_text = _truncate(text, max_chars)
                truncated = hit_byte_cap or final_text != text
                logger.info(
                    "网页抓取完成：url=%s chars=%d truncated=%s", target, len(text), truncated
                )
                return target, final_text, truncated

    raise RedirectLimitExceeded(
        f"重定向超过 {max_redirects} 次，已停止：{url}"
    )


# ------------------------------------------------------------------ 工具构造


def _build_search_tool(config: AppConfig, handler: SearchHandler) -> BaseTool:
    """构造检索工具。

    Args:
        config: 应用配置。
        handler: provider 的实现；由调用方按同一张表解析后传入，避免工具内部
            再判一次 provider 合法性（那会变成一条永远不可达的分支）。
    """
    limit = config.web_search_max_results

    @tool
    async def web_search(query: str) -> str:
        """联网检索：输入关键词，返回若干条结果的标题、链接与摘要。

        摘要只是片段，需要正文时请用 web_fetch 打开其中的链接。
        适合需要时效性信息或本机没有的资料；不要用它查询工作区里已有的文件。
        """
        normalized = (query or "").strip()
        if not normalized:
            raise WebToolError("检索关键词不能为空")

        async with httpx.AsyncClient(
            timeout=config.web_search_timeout_seconds,
            headers={"User-Agent": config.web_user_agent or _DEFAULT_USER_AGENT},
        ) as client:
            try:
                results = await handler(client, config, normalized, limit)
            except httpx.HTTPError as exc:
                raise UpstreamServiceError(f"检索请求失败：{type(exc).__name__}: {exc}") from exc
        return _render_results(normalized, results[:limit])

    return web_search


def _build_fetch_tool(config: AppConfig) -> BaseTool:
    """构造抓取工具。"""
    timeout = config.web_fetch_timeout_seconds
    max_redirects = config.web_fetch_max_redirects
    max_chars = config.web_fetch_max_chars
    user_agent = config.web_user_agent

    @tool
    async def web_fetch(url: str) -> str:
        """抓取一个公开网页并返回其正文文本。

        只支持 http/https 的公网地址：内网、本机、链路本地地址会被出站安全策略
        拒绝（这是刻意设计，不要重试同一类地址）。正文超过长度上限时会截断并标注。
        """
        target = (url or "").strip()
        if not target:
            raise WebToolError("URL 不能为空")
        try:
            final_url, text, _truncated = await _fetch_text(
                target,
                timeout=timeout,
                max_redirects=max_redirects,
                max_chars=max_chars,
                user_agent=user_agent,
            )
        except OutboundAddressRejected as exc:
            raise WebToolError(f"抓取被出站安全策略拒绝：{exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamServiceError(f"抓取请求失败：{type(exc).__name__}: {exc}") from exc

        if final_url != target:
            return f"（经重定向到达 {final_url}）\n\n{text}"
        return text

    return web_fetch


def register_tools(registry: ToolRegistry, config: AppConfig | None = None) -> None:
    """按配置注册联网工具。

    注册规则与模型目录的条件注册同口径：检索工具在「provider 已选且所需配置齐全」
    时才出现，缺失时**不注册并写明原因**；抓取工具不需要密钥，只要模块被加载就注册
    （是否加载该模块本身由 ``CUSTOM_TOOL_MODULES`` 决定）。

    Args:
        registry: 目标注册器。
        config: 应用配置；本模块必须由 ``register_tools(registry, config)`` 形式加载。

    Raises:
        ValueError: 未提供配置。
    """
    if config is None:
        raise ValueError(
            "web_tools 需要配置对象，请以 register_tools(registry, config) 形式加载"
        )

    reason = _search_unavailable_reason(config)
    if reason is None:
        provider = (config.web_search_provider or "none").strip().lower()
        registry.register(
            _build_search_tool(config, _SEARCH_PROVIDERS[provider]),
            source=ToolSource.CUSTOM,
        )
    else:
        # WHY 明确记一条日志：模块被加载了却少一个工具，如果不写原因，运维只能
        # 靠翻源码猜是哪个配置项没填。
        logger.info("联网检索工具未注册：%s", reason)
        if _key_present_but_provider_unset(config):
            # WHY 单独告警：这是实际踩过的坑——密钥已配好，只因 provider 仍是默认
            # ``none``，工具就静默缺席。而「provider 决定调用哪家服务」必须由运营方
            # 显式选择，不能因密钥存在就替他决定，所以只能把这条线索喊出来。
            logger.warning(
                "检测到 WEB_SEARCH_API_KEY 已配置，但 WEB_SEARCH_PROVIDER=none："
                "检索工具不会注册。要启用请把 WEB_SEARCH_PROVIDER 设为 tavily。"
            )

    registry.register(_build_fetch_tool(config), source=ToolSource.CUSTOM)
