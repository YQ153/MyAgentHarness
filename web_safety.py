"""出站地址安全校验（SSRF 防线）。

WHY 需要独立一层而不是在工具里顺手判断：抓取工具的 URL 来自**模型输出**，
而模型输出属于不可信输入。这条防线要挡住的是「让服务替攻击者去访问它够不着
的地址」——云元数据端点（``169.254.169.254``）、内网管理面、本机服务。
把判断集中在一处，才能被单独测试；散在工具里的几行 if 既测不全，也容易被
下一次改动绕过。

**信任边界（重要）**：本模块只用于**模型给出的地址**。运营方在配置里写下的
地址（例如自建 SearXNG 的 ``http://127.0.0.1:8080``）属于受信配置，**不应**
经过本模块——那会把「本机自建服务」这种正当用法一并禁掉。判据是「谁选了这个
地址」，不是「地址长什么样」。

**已覆盖的绕过尝试**：非 HTTP(S) scheme（``file:`` / ``gopher:`` / ``ftp:``）、
十进制与八进制形态的 IP、IPv4-mapped IPv6（``::ffff:127.0.0.1``）、
把公网域名解析到内网地址（DNS 解析后再判一次）。

**未覆盖（诚实声明）**：解析与连接之间的 DNS 重绑定窗口（TOCTOU）——校验用
解析结果判断，而请求按域名重新解析。彻底消除它需要把连接钉在已解析的 IP 上
（并自行处理 TLS SNI 与证书校验），代价远大于收益；本工具的定位是「不给模型
一个方便的内网探测入口」，而不是对抗能控制权威 DNS 的攻击者。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})
"""只允许这两种 scheme。

WHY 白名单而不是黑名单：``file:`` 能读本机文件，``gopher:``/``dict:`` 能构造
任意 TCP 载荷去冲击内网服务。黑名单永远漏，而这里能穷举。
"""


class OutboundAddressRejected(ValueError):
    """目标地址被出站安全策略拒绝。

    WHY 继承 ``ValueError``：调用方（工具层）需要把它与网络故障区分开——
    被拒绝是「这个地址不该访问」，重试没有意义，而超时是可以再试一次的。
    """


def _is_blocked_ip(address: str) -> bool:
    """判断一个 IP 字面量是否不可访问。

    WHY 需要 ``is_global`` 之外的一串判断：``is_global`` 是主判据（它为真才表示
    「已分配给公网」），但它**不覆盖组播**——``224.0.0.1`` 的 ``is_global`` 为真，
    而组播地址显然不该被当作抓取目标。这一点是本模块的用例实测出来的，不是推断的。
    宁可把几条已知的「非单播可达」类别都显式列上：安全判定里的冗余是廉价的，
    漏判不是。

    Args:
        address: IP 字面量字符串。

    Returns:
        ``True`` 表示应当拒绝。
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        # 解析不了说明上游给的不是 IP；此处判「不可访问」是保守选择，
        # 真正的语法校验发生在 URL 解析那一步。
        return True

    # WHY 单独处理 IPv4-mapped IPv6：``::ffff:127.0.0.1`` 若只按 IPv6 判，
    # 取回内嵌的 IPv4 再判一次才能堵掉这个经典绕过。
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped

    return bool(
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
        or not parsed.is_global
    )


def resolve_host_addresses(host: str) -> tuple[str, ...]:
    """把主机名解析为 IP 列表。

    WHY 解析失败按拒绝处理：解析不了就没法判断目标是不是内网，此时放行等于
    把判断权交给运气。工具层本来也需要解析成功才能发出请求，失败即是失败。

    Args:
        host: 主机名或 IP 字面量。

    Returns:
        去重后的 IP 字符串元组。

    Raises:
        OutboundAddressRejected: 解析失败或没有结果。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise OutboundAddressRejected(f"无法解析主机名：{host}（{exc}）") from exc

    addresses: list[str] = []
    for info in infos:
        candidate = str(info[4][0])
        if candidate not in addresses:
            addresses.append(candidate)

    if not addresses:
        raise OutboundAddressRejected(f"主机名没有解析到任何地址：{host}")
    return tuple(addresses)


def validate_outbound_url(url: str) -> str:
    """校验一个待访问的外站地址。

    校验顺序刻意从便宜到昂贵：scheme → 结构 → IP 字面量 → DNS 解析。
    前两步不需要任何 IO，能在绝大多数误输入上直接返回。

    Args:
        url: 待校验的 URL。

    Returns:
        原样返回 URL（本函数只做准入判断，不做规范化）。

    Raises:
        OutboundAddressRejected: scheme 非法、缺少主机名、URL 带凭据，
            或目标指向不可访问的地址。
    """
    if not isinstance(url, str) or not url.strip():
        raise OutboundAddressRejected("URL 不能为空")

    candidate = url.strip()

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise OutboundAddressRejected(f"URL 无法解析：{candidate}") from exc

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise OutboundAddressRejected(
            f"只允许 http/https 地址，实际 scheme：{parts.scheme or '（缺失）'}"
        )

    host = parts.hostname or ""
    if not host:
        raise OutboundAddressRejected(f"URL 缺少主机名：{candidate}")

    if parts.username or parts.password:
        # WHY 拒绝带凭据的 URL：模型没有理由需要它，而 ``http://user@host/``
        # 这种写法最容易被用来伪装真实目标；一旦需要带鉴权的抓取，
        # 应当由运营方在配置里提供请求头，而不是让它出现在模型输出里。
        raise OutboundAddressRejected("URL 不允许携带用户名或密码")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if _is_blocked_ip(str(literal)):
            raise OutboundAddressRejected(f"目标地址不可访问（非公网地址）：{host}")
        return candidate

    for address in resolve_host_addresses(host):
        if _is_blocked_ip(address):
            # WHY 报出解析结果：``example.com`` 解析到 127.0.0.1 时，只说
            # 「被拒绝」会让人以为域名本身非法，而真实原因是解析结果。
            raise OutboundAddressRejected(
                f"目标地址不可访问：{host} 解析到非公网地址 {address}"
            )

    return candidate


__all__ = [
    "OutboundAddressRejected",
    "resolve_host_addresses",
    "validate_outbound_url",
]
