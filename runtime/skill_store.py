"""技能启停状态的持久化。

WHY 状态要落库而不是看目录在不在：技能包（``workspace/skills/<name>/SKILL.md``）只描述
**能力**，它与**是否启用**是两件事。把启用与否实现成「目录在不在」，会让「临时停用一个
技能」变成一次文件搬运，进而在多人环境里变成「一个人的开关移动了所有人共享的文件」。
本表是启用状态的唯一真相，目录只提供候选。

**没有记录即为启用**（``DEFAULT_ENABLED``）。理由与 MCP 的 ``enabled: bool = True`` 同口径：
技能包是用户主动放进来的，放进来的下一刻就期望它能用；默认停用会让人以为「建了却没生效」，
而那是本项目反复要消灭的静默失败。

表里**同时保留显式启用与显式停用**两种记录（而不是「只记停用、启用即删行」）：这样「用户
在这里做过选择」这件事本身是可查的，代价只是一行冗余记录——比「查不到任何痕迹」更容易解释。

``scope`` 列现在恒为 ``global``，但**必须现在就存在**：后续要按场景/会话选择技能集时，
新增的只是取值而不是表结构。同一技能在不同 scope 下互不影响。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiosqlite
from deepagents.middleware.skills import MAX_SKILL_NAME_LENGTH

from runtime.sqlite_lifecycle import open_sqlite_store

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

GLOBAL_SCOPE = "global"
"""当前唯一使用的作用域；其余取值留给后续的场景化选择。"""

DEFAULT_ENABLED = True
"""没有记录时视为启用。"""

_MAX_SCOPE_CHARS = 64
"""作用域标识的长度上限（与技能名同量级，足够表达场景/会话标识）。"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_state (
    scope      TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    enabled    INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, skill_name)
);
"""


def _utc_now() -> str:
    """返回可直接按字典序比较的 ISO8601 UTC 时间串（与其余存储同口径）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SkillStateStore:
    """技能启停状态的读写门面。

    本类只做存取，不判断「这个技能存不存在」——技能包在文件系统上，由
    ``runtime.skills.inspect_skills`` 负责列出。因此本表里可能存在**已不存在的技能**的
    记录（技能包被删掉了），那是正常的：它不影响任何判断，清理也不是必须的。
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        """构造门面（请用 ``open_skill_store``）。

        Raises:
            ValueError: ``conn`` 为 ``None``。
        """
        if conn is None:
            raise ValueError("conn 不能为 None")
        self._conn = conn
        # 与其余存储同一理由：aiosqlite 的连接不允许并发语句交错
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 校验

    @staticmethod
    def _validate_scope(scope: str) -> str:
        """校验作用域标识。

        Raises:
            ValueError: 非字符串、为空或超长。
        """
        if not isinstance(scope, str):
            raise ValueError(f"scope 必须是字符串，实际：{type(scope).__name__}")
        candidate = scope.strip()
        if not candidate:
            raise ValueError("scope 不能为空")
        if len(candidate) > _MAX_SCOPE_CHARS:
            raise ValueError(f"scope 过长（{len(candidate)} > {_MAX_SCOPE_CHARS}）")
        return candidate

    @staticmethod
    def _validate_skill_name(skill_name: str) -> str:
        """校验技能名。

        WHY 这里不重复规范校验（字符集、连字符规则）：那只在 ``runtime.skills`` 里做一次，
        在存储层再做一遍就会有两份口径。这里只挡住「必然无意义」的取值（空、超长），
        长度上限仍取上游常量，不自己写数字。

        Raises:
            ValueError: 非字符串、为空或超长。
        """
        if not isinstance(skill_name, str):
            raise ValueError(f"skill_name 必须是字符串，实际：{type(skill_name).__name__}")
        candidate = skill_name.strip()
        if not candidate:
            raise ValueError("skill_name 不能为空")
        if len(candidate) > MAX_SKILL_NAME_LENGTH:
            raise ValueError(
                f"skill_name 过长（{len(candidate)} > {MAX_SKILL_NAME_LENGTH}）"
            )
        return candidate

    # ------------------------------------------------------------------ 读

    async def enabled_map(self, *, scope: str = GLOBAL_SCOPE) -> dict[str, bool]:
        """返回该作用域下**被显式设置过**的技能启停状态。

        Note:
            没有出现在返回值里的技能按 :data:`DEFAULT_ENABLED` 处理；调用方应使用
            :meth:`is_enabled` 或 :meth:`resolve` 而不是自己补默认值——把默认值散到调用点，
            改默认时就必然漏掉几处。

        Raises:
            ValueError: ``scope`` 非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized = self._validate_scope(scope)
        async with self._lock:
            async with self._conn.execute(
                "SELECT skill_name, enabled FROM skill_state WHERE scope = ?",
                (normalized,),
            ) as cursor:
                rows = await cursor.fetchall()
        return {str(row["skill_name"]): bool(row["enabled"]) for row in rows}

    async def is_enabled(self, skill_name: str, *, scope: str = GLOBAL_SCOPE) -> bool:
        """判断某个技能在该作用域下是否启用（无记录则为启用）。

        Raises:
            ValueError: 参数非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized = self._validate_skill_name(skill_name)
        normalized_scope = self._validate_scope(scope)
        async with self._lock:
            async with self._conn.execute(
                "SELECT enabled FROM skill_state WHERE scope = ? AND skill_name = ?",
                (normalized_scope, normalized),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return DEFAULT_ENABLED
        return bool(row["enabled"])

    async def resolve(
        self, skill_names: list[str], *, scope: str = GLOBAL_SCOPE
    ) -> dict[str, bool]:
        """给一组技能名补上启停状态（无记录的为启用）。

        WHY 批量入口而不是让调用方自己 ``is_enabled`` 逐个查：列出技能时通常要一次性拿到
        全部状态，逐个查既是 N 次往返，也让「默认值怎么补」散到调用点。

        Raises:
            ValueError: 参数非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        names = [self._validate_skill_name(name) for name in skill_names]
        recorded = await self.enabled_map(scope=scope)
        return {name: recorded.get(name, DEFAULT_ENABLED) for name in names}

    async def disabled_names(self, *, scope: str = GLOBAL_SCOPE) -> set[str]:
        """返回该作用域下被显式停用的技能名集合。

        Raises:
            ValueError: ``scope`` 非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        return {
            name for name, enabled in (await self.enabled_map(scope=scope)).items() if not enabled
        }

    # ------------------------------------------------------------------ 写

    async def set_enabled(
        self, skill_name: str, enabled: bool, *, scope: str = GLOBAL_SCOPE
    ) -> dict[str, Any]:
        """设置某个技能的启停状态（UPSERT）。

        WHY 显式写入「启用」而不删除记录：让「用户在这里做过选择」这件事可查。代价是一行
        与默认值重复的记录，而这个代价比「查不到任何痕迹」更值得。

        Args:
            skill_name: 技能名。
            enabled: ``True`` 启用，``False`` 停用。
            scope: 作用域，默认全局。

        Returns:
            写入后的记录（``scope`` / ``skill_name`` / ``enabled`` / ``updated_at``）。

        Raises:
            ValueError: 参数非法（含 ``enabled`` 非布尔）。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized = self._validate_skill_name(skill_name)
        normalized_scope = self._validate_scope(scope)
        if not isinstance(enabled, bool):
            # WHY 单独判类型：``bool`` 是 ``int`` 的子类，而 1 / 0 混进来会让「传错变量」
            # 这件事一路通过——直到某次把字符串 "false" 当成真值用。
            raise ValueError(f"enabled 必须是布尔值，实际：{type(enabled).__name__}")

        now = _utc_now()
        async with self._lock:
            try:
                await self._conn.execute(
                    """
                    INSERT INTO skill_state (scope, skill_name, enabled, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(scope, skill_name) DO UPDATE SET
                        enabled = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (normalized_scope, normalized, 1 if enabled else 0, now),
                )
                await self._conn.commit()
            except Exception:
                logger.exception(
                    "设置技能启停状态失败：scope=%s skill=%s", normalized_scope, normalized
                )
                raise

        logger.info(
            "技能启停状态已更新：scope=%s skill=%s enabled=%s",
            normalized_scope,
            normalized,
            enabled,
        )
        return {
            "scope": normalized_scope,
            "skill_name": normalized,
            "enabled": enabled,
            "updated_at": now,
        }

    async def forget(self, skill_name: str, *, scope: str = GLOBAL_SCOPE) -> bool:
        """删除某个技能的记录，使其回到默认（启用）。

        WHY 与 ``set_enabled(name, True)`` 区分开：后者表达「我选择启用它」，前者表达
        「把这里的选择清掉」。技能包被删除后，清掉它的记录能让表不残留孤儿行。

        Returns:
            是否确实删掉了一行。

        Raises:
            ValueError: 参数非法。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        normalized = self._validate_skill_name(skill_name)
        normalized_scope = self._validate_scope(scope)
        async with self._lock:
            try:
                async with self._conn.execute(
                    "DELETE FROM skill_state WHERE scope = ? AND skill_name = ?",
                    (normalized_scope, normalized),
                ) as cursor:
                    deleted = cursor.rowcount > 0
                await self._conn.commit()
            except Exception:
                logger.exception(
                    "删除技能启停记录失败：scope=%s skill=%s", normalized_scope, normalized
                )
                raise
        return deleted

    # ------------------------------------------------------------------ 探测

    async def ping(self) -> bool:
        """探测数据库连通性。

        Raises:
            RuntimeError: 查询未返回结果行（连接已不可用）。
            aiosqlite.Error: 数据库层异常，原样向上抛出。
        """
        async with self._lock:
            async with self._conn.execute("SELECT 1") as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("技能状态库连通性探测未返回结果行")
        return True


async def _prepare_skill_store(conn: aiosqlite.Connection) -> SkillStateStore:
    """建表并返回存储门面；由 ``open_sqlite_store`` 在初始化阶段调用。"""
    await conn.executescript(_SCHEMA)
    await conn.commit()
    return SkillStateStore(conn)


@asynccontextmanager
async def open_skill_store(db_path: Path) -> AsyncIterator[SkillStateStore]:
    """打开（并按需建表）技能状态库。

    Args:
        db_path: SQLite 文件路径，父目录会自动创建。

    Yields:
        已建表的 ``SkillStateStore``。

    Raises:
        ValueError: ``db_path`` 为 ``None``。
        aiosqlite.Error: 建表或 PRAGMA 设置失败时原样向上抛出。
    """
    async with open_sqlite_store(
        db_path, label="技能状态表", prepare=_prepare_skill_store
    ) as store:
        logger.debug("技能状态表已就绪：%s", db_path)
        yield store
