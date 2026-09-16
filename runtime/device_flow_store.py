"""OIDC Device Authorization Grant 的本地状态存储。

职责边界：只保存 device code / user code 与授权结果之间的映射，
不参与 IdP 侧 device flow。本模块让 CLI 能在无浏览器环境下完成 OIDC 登录。

实现要点：
- device_code 与 user_code 均为高熵随机串，user_code 更短、便于人工输入。
- 状态机：pending → approved / revoked / expired。
- 授权成功后写入 API Key，CLI 轮询时直接取走该 key。
- 过期/轮询超时由调用方清理；本模块提供 cleanup 方法。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_DEVICE_CODE_BYTES = 32
_USER_CODE_BYTES = 4  # 8 位十六进制大写，便于输入

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_flows (
    device_code   TEXT PRIMARY KEY,
    user_code     TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'pending',
    principal     TEXT,
    api_key       TEXT,
    expires_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_polled_at TEXT,
    revoked_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_device_flows_user_code
    ON device_flows (user_code);

CREATE INDEX IF NOT EXISTS idx_device_flows_status_expires
    ON device_flows (status, expires_at);
"""


class DeviceFlowStore:
    """Device Flow 授权状态门面。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        if conn is None:
            raise ValueError("conn 不能为 None")
        self._conn = conn
        self._lock = asyncio.Lock()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _generate_codes(self) -> tuple[str, str]:
        """生成 device_code 与 user_code。"""
        device_code = secrets.token_urlsafe(_DEVICE_CODE_BYTES)
        # user_code 用 8 位大写十六进制，兼顾可读性与输入难度
        user_code = secrets.token_hex(_USER_CODE_BYTES).upper()
        return device_code, user_code

    def _is_expired(self, expires_at: str) -> bool:
        try:
            deadline = datetime.fromisoformat(expires_at)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) > deadline
        except ValueError:
            return True

    async def create(self, *, expires_in_seconds: int = 600) -> dict[str, Any]:
        """创建一条新的 device flow 记录。

        Returns:
            包含 device_code、user_code、expires_at 的字典。
        """
        if expires_in_seconds <= 0:
            raise ValueError("expires_in_seconds 必须为正数")

        device_code, user_code = self._generate_codes()
        now = datetime.now(timezone.utc)
        created_at = now.isoformat(timespec="seconds")
        expires_at = (now + timedelta(seconds=expires_in_seconds)).isoformat(timespec="seconds")

        async with self._lock:
            try:
                await self._conn.execute(
                    """
                    INSERT INTO device_flows
                        (device_code, user_code, status, principal, api_key,
                         expires_at, created_at, last_polled_at, revoked_at)
                    VALUES (?, ?, 'pending', NULL, NULL, ?, ?, NULL, NULL)
                    """,
                    (device_code, user_code, expires_at, created_at),
                )
                await self._conn.commit()
            except Exception:
                logger.exception("Device flow 创建失败")
                raise

        logger.info("Device flow 已创建：device_code=%s... user_code=%s", device_code[:8], user_code)
        return {
            "device_code": device_code,
            "user_code": user_code,
            "expires_at": expires_at,
            "expires_in": expires_in_seconds,
        }

    async def get_by_device_code(self, device_code: str) -> dict[str, Any] | None:
        """按 device_code 读取记录，同时刷新最后轮询时间。"""
        if not device_code:
            return None

        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    SELECT device_code, user_code, status, principal, api_key,
                           expires_at, created_at, last_polled_at, revoked_at
                    FROM device_flows
                    WHERE device_code = ?
                    """,
                    (device_code,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None:
                    await self._conn.execute(
                        "UPDATE device_flows SET last_polled_at = ? WHERE device_code = ?",
                        (self._now(), device_code),
                    )
                    await self._conn.commit()
            except Exception:
                logger.exception("Device flow 读取失败：device_code=%s", device_code[:8])
                raise

        if row is None:
            return None
        return dict(row)

    async def get_by_user_code(self, user_code: str) -> dict[str, Any] | None:
        """按 user_code 读取记录。"""
        if not user_code:
            return None
        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    SELECT device_code, user_code, status, principal, api_key,
                           expires_at, created_at, last_polled_at, revoked_at
                    FROM device_flows
                    WHERE user_code = ?
                    """,
                    (user_code.upper(),),
                ) as cursor:
                    row = await cursor.fetchone()
            except Exception:
                logger.exception("Device flow 读取失败：user_code=%s", user_code)
                raise
        return dict(row) if row else None

    async def approve(
        self,
        user_code: str,
        *,
        principal: dict[str, Any],
        api_key: str,
    ) -> bool:
        """批准指定 user_code，写入主体与 API Key。"""
        if not user_code:
            raise ValueError("user_code 不能为空")
        if not principal:
            raise ValueError("principal 不能为空")
        if not api_key:
            raise ValueError("api_key 不能为空")

        principal_json = json.dumps(principal, ensure_ascii=False)
        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    UPDATE device_flows
                    SET status = 'approved',
                        principal = ?,
                        api_key = ?,
                        last_polled_at = ?
                    WHERE user_code = ? AND status = 'pending'
                    """,
                    (principal_json, api_key, self._now(), user_code.upper()),
                ) as cursor:
                    changed = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception("Device flow 批准失败：user_code=%s", user_code)
                raise

        if changed:
            logger.info("Device flow 已批准：user_code=%s", user_code)
        return changed

    async def revoke(self, device_code: str) -> bool:
        """吊销指定 device flow。"""
        if not device_code:
            raise ValueError("device_code 不能为空")
        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    UPDATE device_flows
                    SET status = 'revoked', revoked_at = ?
                    WHERE device_code = ?
                    """,
                    (self._now(), device_code),
                ) as cursor:
                    changed = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception("Device flow 吊销失败：device_code=%s", device_code[:8])
                raise
        return changed

    async def cleanup(self, max_age_seconds: int = 86400) -> int:
        """清理过期或完成超过 max_age_seconds 的记录。"""
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds 不能为负数")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        ).isoformat(timespec="seconds")
        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    DELETE FROM device_flows
                    WHERE expires_at < ?
                       OR (status IN ('approved', 'revoked')
                           AND last_polled_at IS NOT NULL
                           AND last_polled_at < ?)
                    """,
                    (cutoff, cutoff),
                ) as cursor:
                    count = cursor.rowcount
                await self._conn.commit()
            except Exception:
                logger.exception("Device flow 清理失败")
                raise
        if count:
            logger.info("已清理 %d 条 device flow 记录", count)
        return count


@asynccontextmanager
async def open_device_flow_store(db_path: Path) -> AsyncIterator[DeviceFlowStore]:
    """以异步上下文方式提供 Device Flow 存储。"""
    if db_path is None:
        raise ValueError("db_path 不能为 None")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn: aiosqlite.Connection | None = None
    try:
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        logger.info("Device flow 表已就绪：%s", db_path)
        yield DeviceFlowStore(conn)
    except Exception:
        logger.exception("Device flow 表初始化失败：%s", db_path)
        raise
    finally:
        if conn is not None:
            await conn.close()
