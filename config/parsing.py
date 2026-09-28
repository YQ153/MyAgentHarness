"""配置值的解析与判定工具：列表型配置的统一口径、目录名清洗与回环地址判定。

WHY 独立成模块：这些是**纯函数**（不依赖任何配置字段），被多个域的
validator（``settings.*``）与派生逻辑（``app_config`` / ``session_root``）
共用。放在任何一个使用方旁边都会造成包内依赖分叉；收在最底层，口径的
「唯一出处」才成立。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_UNSAFE_DIR_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _readable_dir_name(root: Path, *, limit: int = 32) -> str:
    """把工作区路径的最后一段压成一个可安全用作目录名的可读片段。

    WHY 要可读：``roots/`` 下会住着十几个存储目录，纯哈希名在排障时无法对应回工作区。
    WHY 还要截断与清洗：目录名可能含空格、中文、超长路径段，直接拼进路径既可能超出平台
    上限，也可能与分隔符撞上。

    Args:
        root: 工作区根目录。
        limit: 片段的最大字符数。

    Returns:
        仅含 ``[A-Za-z0-9._-]`` 的片段；清洗后为空时返回 ``root``。
    """
    cleaned = _UNSAFE_DIR_NAME.sub("_", root.name.strip())[:limit].strip("._")
    return cleaned or "root"


def parse_list_config(value: object, *, field: str, separators: tuple[str, ...]) -> object:
    """把「列表型配置」的原始值解析成序列。

    WHY 需要 ``NoDecode`` + 本函数：``pydantic-settings`` 对 ``list[...]`` 这类
    复杂类型默认按 JSON 解码，``SANDBOX_ENV_ALLOWLIST=PATH,TEMP`` 会在加载期
    抛一条与用户意图无关的 JSON 解析错误，而分隔符写法才是 shell 与 ``.env``
    里的常规写法（``PATH`` 本身即如此）。这里同时接受两种形态：以 ``[`` 开头
    按 JSON 解析，否则按分隔符切分。

    WHY 解析权收在本函数而不是每个字段各写一份：四个列表型字段（工具模块、
    MCP 清单、环境白名单、技能目录）需要完全一致的「空值、空白、非法类型」
    口径，各写一份迟早会出现「某个字段把空串当成一个有效项」这类偏差。

    Args:
        value: 原始值，可能是环境变量字符串、已构造好的序列或 ``None``。
        field: 字段名，仅用于错误信息与日志定位。
        separators: 允许的分隔符，**第一个为主分隔符**。普通字符串列表用
            ``,``；路径列表用 ``os.pathsep``（Windows 为 ``;``，POSIX 为 ``:``
            ——路径本身可能含逗号，不能拿逗号当路径分隔符）。

    Returns:
        解析后的列表；``None`` 与空串都归一为空列表，空白项被剔除。
        序列入参原样转成 ``list``，交由字段注解做元素级校验。

    Raises:
        ValueError: ``separators`` 为空；字符串以 ``[`` 开头但不是合法 JSON
            数组；类型既非字符串也非序列。
    """
    if not separators:
        msg = "separators 至少需要一个分隔符"
        logger.error("%s：%s", field, msg)
        raise ValueError(f"{msg}（字段：{field}）")
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                msg = f"{field} 不是合法 JSON 数组：{exc}"
                logger.error("%s", msg)
                raise ValueError(msg) from exc
            logger.debug("配置项 %s 按 JSON 解析出 %d 项", field, len(decoded))
            return decoded
        normalized = text
        for separator in separators[1:]:
            normalized = normalized.replace(separator, separators[0])
        items = [item.strip() for item in normalized.split(separators[0]) if item.strip()]
        logger.debug("配置项 %s 按分隔符 %r 解析出 %d 项", field, separators[0], len(items))
        return items
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    msg = f"{field} 必须是字符串或序列，实际：{type(value).__name__}"
    logger.error("%s", msg)
    raise ValueError(msg)


def is_loopback_host(host: str | None) -> bool:
    """判断监听地址是否只对本机可达。

    WHY 用 ``ipaddress`` 而不是字符串白名单：``127.0.0.53``、``::1`` 与
    ``[::1]`` 都是回环，逐个枚举必然漏；漏掉一个的后果是把本来安全的绑定
    报成「暴露」，而这类误报出现几次之后，告警就会被当成噪音忽略——真正
    危险的那次会一起被忽略。

    Args:
        host: 监听地址或主机名，允许 ``None``（未配置）。

    Returns:
        True 表示可判定为回环（IP 回环段，或 ``localhost``）；
        ``None`` / 空白 / 无法解析的主机名一律返回 False——无法证明是本机时
        按「可能对外」处理，宁可误报不可漏报。

    Raises:
        ValueError: ``host`` 既非 ``None`` 也非字符串。
    """
    if host is None:
        return False
    if not isinstance(host, str):
        msg = f"host 必须是字符串或 None，实际：{type(host).__name__}"
        logger.error("%s", msg)
        raise ValueError(msg)

    candidate = host.strip()
    if not candidate:
        return False
    # ``[::1]`` 是 URL 里的 IPv6 字面量写法，直接交给 ip_address 会解析失败
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1].strip()

    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return candidate.lower() == "localhost"
