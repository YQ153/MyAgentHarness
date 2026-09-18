"""历史会话所有权迁移脚本。

用途：开启认证后，把已有 thread_meta.owner_id = '' 的会话全部归属到指定用户，
避免旧会话在认证开启后「消失」。

用法：

    python scripts/migrate_thread_owners.py --db-path ./.data/agent.db --owner-id admin@example.com [--dry-run]

注意事项：
- 该脚本直接修改 SQLite，运行前建议备份数据库。
- 无法推断旧会话的真实所有者，必须由管理员显式指定接收人；
  多用户历史数据应结合业务日志人工拆分后分批执行。
- 已存在 owner_id 的会话不会被覆盖。
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把未归属历史会话迁移到指定 owner_id"
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="SQLite 数据库路径",
    )
    parser.add_argument(
        "--owner-id",
        required=True,
        help="目标所有者标识，例如 apikey:dev 或 apikey:<key_id>",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计不写入",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="日志级别",
    )
    return parser.parse_args()


def ensure_owner_column(conn: sqlite3.Connection) -> None:
    """确保 owner_id 列存在；不存在时创建。"""
    cur = conn.execute("PRAGMA table_info(thread_meta)")
    columns = {row[1] for row in cur.fetchall()}
    if "owner_id" not in columns:
        logger.info("thread_meta 表缺少 owner_id 列，自动创建")
        conn.execute("ALTER TABLE thread_meta ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
        conn.commit()


def migrate(db_path: Path, owner_id: str, *, dry_run: bool) -> int:
    """执行迁移并返回更新的会话数。"""
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_owner_column(conn)

        cursor = conn.execute(
            "SELECT COUNT(1) FROM thread_meta WHERE owner_id = '' OR owner_id IS NULL"
        )
        total_unowned = cursor.fetchone()[0]

        if dry_run:
            logger.info("[DRY-RUN] 发现 %d 条未归属会话，将迁移到 owner_id=%s", total_unowned, owner_id)
            return 0

        if total_unowned == 0:
            logger.info("没有需要迁移的未归属会话")
            return 0

        cursor = conn.execute(
            "UPDATE thread_meta SET owner_id = ? WHERE owner_id = '' OR owner_id IS NULL",
            (owner_id,),
        )
        conn.commit()
        updated = cursor.rowcount
        logger.info("已迁移 %d / %d 条未归属会话到 owner_id=%s", updated, total_unowned, owner_id)
        return updated
    finally:
        conn.close()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    db_path = Path(args.db_path).expanduser().resolve()
    if not db_path.exists():
        logger.error("数据库不存在：%s", db_path)
        return 2

    try:
        updated = migrate(db_path, args.owner_id, dry_run=args.dry_run)
    except sqlite3.Error as exc:
        logger.exception("数据库操作失败")
        return 1
    except Exception:
        logger.exception("迁移失败")
        return 1

    logger.info("迁移完成，共更新 %d 条会话", updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
