"""认证子包内的通用工具函数。

只包含无状态的纯函数：请求头解析。这些函数被 ``audit``、``deps`` 等多个模块共用，
独立出来避免它们互相导入。
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
