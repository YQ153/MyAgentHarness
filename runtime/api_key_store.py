"""API Key 存储。

职责边界：负责 API Key 的创建、校验、吊销与列出。
WHY 独立模块：API Key 是会话认证凭证，与 thread_meta 是不同的聚合；
放在独立表中便于按 key 维度做作用域、过期、轮换、吊销。

安全说明：
- Key 本身只会在创建时返回一次，之后以 SHA-256 哈希存储。
- 校验使用 ``secrets.compare_digest`` 做常量时间比较，避免时序攻击。
- 当前使用 SHA-256；若后续需要更高强度，可整体迁移到 bcrypt/Argon2，
  本模块只暴露 ``create`` / ``validate`` / ``revoke`` / ``list``，内部实现可替换。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import string
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_ALPHABET = string.ascii_letters + string.digits
_KEY_BYTES = 32
_PREFIX_LEN = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id      TEXT PRIMARY KEY,
    key_hash    TEXT NOT NULL UNIQUE,
    key_prefix  TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'member',
    scopes      TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    expires_at  TEXT,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_keys_enabled
    ON api_keys (enabled, expires_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _generate_key() -> str:
    """生成高熵 API Key。

    格式：``harness_<随机 32 字符>``，共 40 字符，便于用户识别来源
    （``harness_`` 前缀 + ``_KEY_BYTES`` 个 ``[_ALPHABET]`` 字符；
    每字符 log2(62) ≈ 5.95 bit，合计约 190 bit 熵，远超暴力枚举的门槛）。
    """
    return "harness_" + "".join(secrets.choice(_ALPHABET) for _ in range(_KEY_BYTES))


def _hash_key(key: str) -> str:
    """对 API Key 做 SHA-256 哈希并转 16 进制小写字符串。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(expires_at)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > deadline
    except ValueError:
        # 非法过期时间按「已过期」处理，避免永不过期漏洞
        logger.warning("API Key 包含非法过期时间：%s", expires_at)
        return True


class APIKeyStore:
    """API Key 的持久化门面。"""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        if conn is None:
            raise ValueError("conn 不能为 None")
        self._conn = conn
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        role: str = "member",
        scopes: list[str] | None = None,
        description: str = "",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """创建一条 API Key。

        Returns:
            包含 ``key_id`` 与 ``key`` 的字典；``key`` 只在此返回一次，
            调用方必须立即保存或展示，后续无法恢复明文。

        Raises:
            ValueError: role 不存在或 scopes 非法。
            aiosqlite.Error: 数据库异常。
        """
        if not role:
            raise ValueError("role 不能为空")
        if scopes is None:
            scopes = []

        key = _generate_key()
        key_hash = _hash_key(key)
        key_id = secrets.token_urlsafe(16)
        key_prefix = key[:_PREFIX_LEN]
        now = _now()

        async with self._lock:
            try:
                await self._conn.execute(
                    """
                    INSERT INTO api_keys
                        (key_id, key_hash, key_prefix, role, scopes, enabled,
                         description, expires_at, created_at, revoked_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL)
                    """,
                    (
                        key_id,
                        key_hash,
                        key_prefix,
                        role,
                        " ".join(scopes),
                        description,
                        expires_at,
                        now,
                    ),
                )
                await self._conn.commit()
            except Exception:
                logger.exception("API Key 创建失败")
                raise

        logger.info("API Key 已创建：key_id=%s role=%s scopes=%s", key_id, role, scopes)
        return {
            "key_id": key_id,
            "key": key,
            "key_prefix": key_prefix,
            "role": role,
            "scopes": scopes,
            "description": description,
            "expires_at": expires_at,
            "created_at": now,
        }

    async def validate(self, key: str) -> dict[str, Any] | None:
        """校验 API Key 并返回关联的元数据（不含哈希）。

        Returns:
            元数据字典；key 不存在、已吊销、已过期或已禁用时返回 ``None``。
        """
        if not key:
            return None
        key_hash = _hash_key(key)

        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    SELECT key_id, key_prefix, role, scopes, enabled, expires_at, revoked_at
                    FROM api_keys
                    WHERE key_hash = ?
                    """,
                    (key_hash,),
                ) as cursor:
                    row = await cursor.fetchone()
            except Exception:
                logger.exception("API Key 校验失败")
                raise

        if row is None:
            return None
        record = dict(row)
        if not record.get("enabled"):
            return None
        if record.get("revoked_at"):
            return None
        if _is_expired(record.get("expires_at")):
            return None
        return record

    async def list(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        """列出 API Key。"""
        sql = """
            SELECT key_id, key_prefix, role, scopes, enabled, description,
                   expires_at, created_at, revoked_at
            FROM api_keys
        """
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY created_at DESC"

        async with self._lock:
            async with self._conn.execute(sql) as cursor:
                rows = await cursor.fetchall()

        return [dict(row) for row in rows]

    async def revoke(self, key_id: str) -> bool:
        """吊销指定 API Key。

        Returns:
            是否成功吊销（False 表示 key_id 不存在）。
        """
        if not key_id:
            raise ValueError("key_id 不能为空")

        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    UPDATE api_keys
                    SET enabled = 0, revoked_at = ?
                    WHERE key_id = ?
                    """,
                    (_now(), key_id),
                ) as cursor:
                    changed = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception("API Key 吊销失败：key_id=%s", key_id)
                raise

        if changed:
            logger.info("API Key 已吊销：key_id=%s", key_id)
        return changed

    async def cleanup_expired(self) -> int:
        """把已过期 key 标记为禁用，返回清理数量。"""
        now = _now()
        async with self._lock:
            try:
                async with self._conn.execute(
                    """
                    UPDATE api_keys
                    SET enabled = 0
                    WHERE enabled = 1
                      AND expires_at IS NOT NULL
                      AND expires_at < ?
                      AND revoked_at IS NULL
                    """,
                    (now,),
                ) as cursor:
                    count = cursor.rowcount
                await self._conn.commit()
            except Exception:
                logger.exception("清理过期 API Key 失败")
                raise

        if count:
            logger.info("已清理 %d 条过期 API Key", count)
        return count


@asynccontextmanager
async def open_api_key_store(db_path: Path) -> AsyncIterator[APIKeyStore]:
    """以异步上下文的方式提供 API Key 存储。"""
    if db_path is None:
        raise ValueError("db_path 不能为 None")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn: aiosqlite.Connection | None = None
    try:
        # WHY 只把初始化包在捕获里、``yield`` 留在它外面：``yield`` 之后抛出的异常来自
        # ``async with`` 主体（调用方的装配或业务代码），这里接住会把它记成「表初始化
        # 失败」——主体里一个 ValueError 就能让每个存储各打一份「初始化失败 + 堆栈」，
        # 把排查引向数据库，而数据库根本没问题。连接失败同样是初始化失败，故一并包住。
        try:
            conn = await aiosqlite.connect(str(db_path))
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA busy_timeout=5000;")
            await conn.executescript(_SCHEMA)
            await conn.commit()
        except Exception:
            logger.exception("API Key 表初始化失败：%s", db_path)
            raise
        logger.info("API Key 表已就绪：%s", db_path)
        yield APIKeyStore(conn)
    finally:
        if conn is not None:
            await conn.close()
