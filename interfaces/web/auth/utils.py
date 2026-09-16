"""认证子包内的通用工具函数。

只包含无状态的纯函数：请求头解析与重定向目标校验。这些函数被 ``audit``、
``deps``、``flow`` 多个模块共用，独立出来避免它们互相导入。
"""

from __future__ import annotations

from fastapi import Request


def client_ip(request: Request) -> str:
    """获取客户端 IP，优先读取反向代理透传头。

    WHY 优先读 ``X-Forwarded-For``：应用部署在反向代理后方时，
    ``request.client.host`` 只会是代理自身地址，无法用于限流与审计定位。
    注意该头可被客户端伪造，因此只用于「限流与审计」这类可容忍偏差的场景。

    Args:
        request: 当前请求。

    Returns:
        客户端 IP；无法确定时返回 ``"unknown"``。
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-Ip")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return "unknown"


def user_agent(request: Request) -> str:
    """提取 User-Agent，缺失时返回空串。"""
    return request.headers.get("user-agent", "") or ""


def safe_next(next_url: str | None) -> str:
    """校验登录后的跳转目标，防止开放重定向。

    WHY 只允许以单个 ``/`` 开头：``//evil.com`` 会被浏览器解释为协议相对
    URL 而跳出本站，因此必须同时排除双斜杠开头的情况。

    Args:
        next_url: 来自查询参数的原始跳转目标。

    Returns:
        合法的同源路径；非法或为空时返回 ``"/"``。
    """
    if not next_url:
        return "/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


def bearer_token(request: Request) -> str | None:
    """从 ``Authorization: Bearer`` 头中提取 token。

    Args:
        request: 当前请求。

    Returns:
        去除首尾空白的 token；未提供或为空时返回 ``None``。
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip() or None
