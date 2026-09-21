"""出站地址安全校验的回归测试（SSRF 防线）。

WHY 必须逐条钉住：这一层的输入来自模型输出（不可信），而它的典型失败方式是
「静默放行」——放行一次内网访问不会抛错、不会留痕，只会在某个时刻被别人利用。
安全边界靠读代码「相信它对」是不够的，必须有用例把每条已知绕过路径固定下来。
"""

from __future__ import annotations

import socket

import pytest

from web_safety import (
    OutboundAddressRejected,
    _is_blocked_ip,
    resolve_host_addresses,
    validate_outbound_url,
)

_BLOCKED_LITERALS = [
    ("http://127.0.0.1/admin", "回环"),
    ("http://127.1.2.3/", "整个 127/8"),
    ("http://0.0.0.0/", "未指定地址"),
    ("http://10.0.0.1/", "私网 A"),
    ("http://172.16.0.1/", "私网 B"),
    ("http://192.168.1.1/", "私网 C"),
    ("http://169.254.169.254/latest/meta-data/", "云元数据端点"),
    ("http://100.64.0.1/", "CGNAT"),
    ("http://198.18.0.1/", "保留段（基准测试）"),
    ("http://224.0.0.1/", "组播"),
    ("http://239.255.255.250/", "SSDP 组播"),
    ("http://255.255.255.255/", "广播"),
    ("http://[ff02::1]/", "IPv6 组播"),
    ("http://[::1]/", "IPv6 回环"),
    ("http://[fc00::1]/", "IPv6 ULA"),
    ("http://[fe80::1]/", "IPv6 链路本地"),
    ("http://[::ffff:127.0.0.1]/", "IPv4-mapped IPv6 回环"),
    ("http://[::ffff:10.0.0.1]/", "IPv4-mapped IPv6 私网"),
]

_PUBLIC_LITERALS = [
    "http://8.8.8.8/",
    "https://1.1.1.1/dns-query",
    "http://[2606:4700::1111]/",
]

_NON_HTTP_SCHEMES = [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/",
    "data:text/plain,hi",
    "javascript:alert(1)",
]


@pytest.mark.parametrize(("url", "reason"), _BLOCKED_LITERALS)
def test_rejects_non_public_addresses(url: str, reason: str):
    with pytest.raises(OutboundAddressRejected, match="非公网地址"):
        validate_outbound_url(url)


@pytest.mark.parametrize("url", _PUBLIC_LITERALS)
def test_allows_public_literal_addresses(url: str):
    # 字面量是公网地址时不应触发 DNS 解析：判断只需要前两步
    assert validate_outbound_url(url) == url


@pytest.mark.parametrize("url", _NON_HTTP_SCHEMES)
def test_rejects_non_http_schemes(url: str):
    """``file:`` 能读本机文件，``gopher:`` 能构造任意 TCP 载荷——白名单之外一律拒绝。"""
    with pytest.raises(OutboundAddressRejected, match="http/https"):
        validate_outbound_url(url)


@pytest.mark.parametrize("url", ["", "   ", "not-a-url", "http://", "https:///path"])
def test_rejects_malformed_urls(url: str):
    with pytest.raises(OutboundAddressRejected):
        validate_outbound_url(url)


def test_rejects_none_url():
    with pytest.raises(OutboundAddressRejected, match="不能为空"):
        validate_outbound_url(None)  # type: ignore[arg-type]


def test_rejects_url_with_invalid_ipv6_bracket():
    """``urlsplit`` 对残缺的 IPv6 字面量会抛 ValueError，不能让它冒到工具层。"""
    with pytest.raises(OutboundAddressRejected, match="无法解析"):
        validate_outbound_url("http://[::1/")


def test_blocked_ip_treats_unparseable_input_as_blocked():
    """契约：解析不了按「不可访问」处理。

    WHY 单独钉住这条：函数是纯判定，输入来自解析结果，正常路径永远是合法 IP；
    但它对非法输入的约定方向决定出错时倒向哪一侧——安全判定必须倒在拒绝一侧。"""
    assert _is_blocked_ip("not-an-ip") is True


def test_rejects_credentials_in_url():
    """``http://user@host/`` 是最容易用来伪装真实目标的写法，而模型没有理由需要它。"""
    with pytest.raises(OutboundAddressRejected, match="用户名或密码"):
        validate_outbound_url("http://user:pass@example.com/")


def test_rejects_hostname_resolving_to_private_address(monkeypatch):
    """公网域名指向内网是最常见的 SSRF 绕过：只看字面量判断会直接放行。"""
    monkeypatch.setattr("web_safety.resolve_host_addresses", lambda host: ("127.0.0.1",))

    with pytest.raises(OutboundAddressRejected, match="127.0.0.1"):
        validate_outbound_url("http://internal.example.com/")


def test_allows_hostname_resolving_to_public_address(monkeypatch):
    monkeypatch.setattr("web_safety.resolve_host_addresses", lambda host: ("93.184.216.34",))

    assert validate_outbound_url("https://example.com/page") == "https://example.com/page"


def test_rejects_when_any_resolved_address_is_private(monkeypatch):
    """一台主机解析出多个地址时，只要有一个是内网就必须拒绝。

    WHY 不能只看第一个：攻击者若能影响解析顺序，就能让「首个结果是公网」的判定
    变成随机放行。"""
    monkeypatch.setattr(
        "web_safety.resolve_host_addresses",
        lambda host: ("93.184.216.34", "10.0.0.5"),
    )

    with pytest.raises(OutboundAddressRejected, match="10.0.0.5"):
        validate_outbound_url("http://multi.example.com/")


def test_resolve_failure_is_rejected(monkeypatch):
    """解析不了就判断不了是不是内网，此时放行等于把判断权交给运气。"""

    def _fail(*args: object, **kwargs: object) -> None:
        raise socket.gaierror("getaddrinfo failed")

    monkeypatch.setattr("web_safety.socket.getaddrinfo", _fail)

    with pytest.raises(OutboundAddressRejected, match="无法解析"):
        validate_outbound_url("http://no-such-host.invalid/")
    with pytest.raises(OutboundAddressRejected, match="无法解析"):
        resolve_host_addresses("no-such-host.invalid")


def test_resolve_host_addresses_deduplicates(monkeypatch):
    infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 0, 0, 0)),
    ]
    monkeypatch.setattr("web_safety.socket.getaddrinfo", lambda *args, **kwargs: infos)

    assert resolve_host_addresses("example.com") == ("93.184.216.34", "2606:4700::1111")


def test_resolve_rejects_empty_answer(monkeypatch):
    monkeypatch.setattr("web_safety.socket.getaddrinfo", lambda *args, **kwargs: [])

    with pytest.raises(OutboundAddressRejected, match="没有解析到任何地址"):
        resolve_host_addresses("empty.example.com")
