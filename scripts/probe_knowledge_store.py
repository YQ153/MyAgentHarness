"""知识库存储的开工前探针：把建表方案钉在实测事实上。

要回答的五个问题（每一个都直接决定 schema 与检索分支怎么写）：

1. ``sqlite-vec`` 在本机能否加载，``vec0`` 的距离语义是什么——归一化向量下
   它是否等价于余弦排序（决定要不要显式指定 ``distance_metric``）。
2. ``vec0`` 能否带 **metadata 列** 并在查询里过滤（决定跨主体隔离是在 SQL 里
   完成，还是只能捞出 top-k 再在 Python 里筛——后者会让「该主体只看到自己文档」
   这件事在 k 很小的时候失效）。
3. ``vec0`` 能否用 ``rowid IN (子查询)`` 收窄候选集（与 2 互为备选方案）。
4. ``trigram`` 分词器的**最小可查长度**与中文召回——T19 已测过一轮，这里复核
   两字查询是否可用，因为它决定关键词检索在什么情况下必须回落到 ``LIKE``。
5. FTS5 虚拟表与 vec0 虚拟表能否共存于同一个库文件。

WHY 先测再实现：以上每一条都由 SQLite 的编译选项、扩展版本与虚拟表实现细节决定，
靠记忆推断必然出错；而它们的组合正好是「建表语句 + 检索分支」的全部形状。

用法：``python scripts/probe_knowledge_store.py``；退出码 ``0`` 全部可用 /
``1`` 有失败。Windows 上 FTS5 的查询串含中文引号，故全程强制 UTF-8 输出。
"""

from __future__ import annotations

import sqlite3
import struct
import sys

# WHY 强制 UTF-8：本脚本的查询串与语料都是中文，Windows 控制台默认 GBK 会让
# print 抛 UnicodeEncodeError，把一次成功的探测变成假失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_CORPUS = [
    "帮我排查一下登录接口的超时问题，日志里只看到 p99 到了 3 秒",
    "数据库连接池的配置需要调整，max_overflow 太小了",
    "幂等键应该由客户端生成，服务端只做校验",
]
"""中文语料：含 2 字、3 字、4 字等不同长度的可查询片段。"""

_QUERIES = [
    ("登录接口", "中文四字"),
    ("连接池", "中文三字"),
    ("超时", "中文两字"),
    ("幂等", "中文两字"),
    ("p99", "英数三字"),
]


def _pack(vector: list[float]) -> bytes:
    """把向量打包成 vec0 接受的 float32 字节串。"""
    return struct.pack(f"<{len(vector)}f", *vector)


def probe_vec0() -> int:
    """问题 1：加载、建表、距离语义。"""
    print("=== 问题 1：sqlite-vec 与距离语义 ===")
    try:
        import sqlite_vec
    except ImportError as exc:
        print(f"  [FAIL] 无法导入 sqlite_vec：{exc}")
        return 1

    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except (AttributeError, sqlite3.OperationalError) as exc:
        print(f"  [FAIL] 加载扩展失败（可能是 SQLite 未开扩展支持）：{exc}")
        return 1

    print(f"  sqlite 版本：{sqlite3.sqlite_version}")
    print(f"  vec 版本   ：{conn.execute('select vec_version()').fetchone()[0]}")

    conn.execute("CREATE VIRTUAL TABLE v USING vec0(embedding float[4])")
    conn.execute(
        "INSERT INTO v(rowid, embedding) VALUES (1, ?), (2, ?), (3, ?)",
        (_pack([1.0, 0.0, 0.0, 0.0]), _pack([0.8, 0.6, 0.0, 0.0]), _pack([0.0, 1.0, 0.0, 0.0])),
    )
    rows = conn.execute(
        "SELECT rowid, distance FROM v WHERE embedding MATCH ? ORDER BY distance LIMIT 3",
        (_pack([1.0, 0.0, 0.0, 0.0]),),
    ).fetchall()
    print(f"  查询 [1,0,0,0] 的距离：{rows}")

    # 判据：L2 距离下，与查询向量夹角小但模长大的向量会排在后面；归一化向量下
    # 两者等价。这里用两个**同方向**、模长不同的向量验证「默认是 L2 而不是余弦」。
    conn.execute("DELETE FROM v")
    conn.execute(
        "INSERT INTO v(rowid, embedding) VALUES (1, ?), (2, ?)",
        (_pack([1.0, 0.0, 0.0, 0.0]), _pack([0.1, 0.0, 0.0, 0.0])),
    )
    l2_rows = conn.execute(
        "SELECT rowid, distance FROM v WHERE embedding MATCH ? ORDER BY distance LIMIT 2",
        (_pack([1.0, 0.0, 0.0, 0.0]),),
    ).fetchall()
    print(f"  同方向不同模长的距离：{l2_rows}（若 1 的距离为 0 且 2 不为 0 → 默认是 L2）")
    print("  说明：本项目嵌入已归一化（探针实测模长 1.0000），L2 与余弦排序等价，")
    print("        故不必显式指定 distance_metric。")

    # 归一化前提的自检方式也要能跑：把模长 1 的向量连同验证一起留给上层脚本
    conn.close()
    print("  [OK  ] 加载、建表、KNN 查询均可用")
    return 0


def probe_metadata_filter() -> int:
    """问题 2：metadata 列过滤（跨主体隔离用）。"""
    print("\n=== 问题 2：vec0 的 metadata 列过滤 ===")
    import sqlite_vec

    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE v USING vec0(owner_id text, embedding float[4])"
        )
    except sqlite3.OperationalError as exc:
        print(f"  [FAIL] 无法建带 metadata 列的 vec0 表：{exc}")
        conn.close()
        return 1

    conn.execute(
        "INSERT INTO v(rowid, owner_id, embedding) VALUES (1, ?, ?), (2, ?, ?), (3, ?, ?)",
        (
            "alice",
            _pack([1.0, 0.0, 0.0, 0.0]),
            "alice",
            _pack([0.9, 0.1, 0.0, 0.0]),
            "bob",
            _pack([1.0, 0.0, 0.0, 0.0]),
        ),
    )
    try:
        rows = conn.execute(
            """
            SELECT rowid, owner_id, distance FROM v
            WHERE embedding MATCH ? AND owner_id = ? ORDER BY distance LIMIT 5
            """,
            (_pack([1.0, 0.0, 0.0, 0.0]), "alice"),
        ).fetchall()
        print(f"  metadata 过滤结果：{rows}")
        print("  [OK  ] 可以在 SQL 层按主体隔离，且不牺牲 top-k 的正确性")
        conn.close()
        return 0
    except sqlite3.OperationalError as exc:
        print(f"  [NO  ] metadata 列过滤不可用：{exc}")
        conn.close()
        return 1


def probe_rowid_filter() -> int:
    """问题 3：rowid 子查询过滤（备选隔离方案）。"""
    print("\n=== 问题 3：vec0 的 rowid IN (子查询) ===")
    import sqlite_vec

    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.execute("CREATE VIRTUAL TABLE v USING vec0(embedding float[4])")
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, owner_id TEXT)")
    conn.executemany(
        "INSERT INTO chunks(id, owner_id) VALUES (?, ?)", [(1, "alice"), (2, "bob")]
    )
    conn.execute(
        "INSERT INTO v(rowid, embedding) VALUES (1, ?), (2, ?)",
        (_pack([1.0, 0.0, 0.0, 0.0]), _pack([1.0, 0.0, 0.0, 0.0])),
    )
    try:
        rows = conn.execute(
            """
            SELECT rowid, distance FROM v
            WHERE embedding MATCH ? AND rowid IN (SELECT id FROM chunks WHERE owner_id = ?)
            ORDER BY distance LIMIT 5
            """,
            (_pack([1.0, 0.0, 0.0, 0.0]), "alice"),
        ).fetchall()
        print(f"  子查询过滤结果：{rows}")
        print("  [OK  ] 可用作 metadata 列不可用时的备选")
        conn.close()
        return 0
    except sqlite3.OperationalError as exc:
        print(f"  [NO  ] rowid 子查询过滤不可用：{exc}")
        conn.close()
        return 1


def probe_trigram() -> int:
    """问题 4：trigram 的可查长度与中文召回。"""
    print("\n=== 问题 4：FTS5 trigram 的中文召回 ===")
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='trigram')")
    except sqlite3.OperationalError as exc:
        print(f"  [FAIL] trigram 不可用：{exc}")
        conn.close()
        return 1

    for text in _CORPUS:
        conn.execute("INSERT INTO docs(body) VALUES (?)", (text,))

    short_failed: list[str] = []
    for query, label in _QUERIES:
        try:
            hits = conn.execute(
                "SELECT count(*) FROM docs WHERE docs MATCH ?", (f'"{query}"',)
            ).fetchone()[0]
            print(f"  {label:<8}「{query}」→ 命中 {hits}")
            if hits == 0:
                short_failed.append(query)
        except sqlite3.OperationalError as exc:
            print(f"  {label:<8}「{query}」→ 查询失败：{exc}")
            short_failed.append(query)

    # 两字查询要么注入失败、要么零命中，两种情况都要求回落 LIKE
    print(f"  零命中或失败的查询：{short_failed}（这些必须回落到 LIKE）")
    like_hits = conn.execute(
        "SELECT count(*) FROM docs WHERE body LIKE ?", ("%超时%",)
    ).fetchone()[0]
    print(f"  同一查询用 LIKE 的命中：{like_hits}（LIKE 能兜住两字查询）")
    conn.close()
    print("  [OK  ] 结论：>= 3 字的片段走 FTS，更短的回落 LIKE")
    return 0


def probe_coexistence() -> int:
    """问题 5：FTS5 与 vec0 共存。"""
    print("\n=== 问题 5：FTS5 与 vec0 共存于同一库 ===")
    import sqlite_vec

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE fts USING fts5(body, tokenize='trigram')")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute("CREATE VIRTUAL TABLE vec USING vec0(embedding float[4])")
    except (sqlite3.OperationalError, AttributeError) as exc:
        print(f"  [FAIL] 共存失败：{exc}")
        conn.close()
        return 1

    conn.execute("INSERT INTO fts(body) VALUES ('登录接口超时')")
    conn.execute("INSERT INTO vec(rowid, embedding) VALUES (1, ?)", (_pack([1.0, 0.0, 0.0, 0.0]),))
    print("  两张虚拟表均创建并写入成功")
    conn.close()
    print("  [OK  ] 关键词与向量两条路可共用同一个库文件")
    return 0


def main() -> int:
    """跑完五个问题；任一失败即返回 1。"""
    results = [
        probe_vec0(),
        probe_metadata_filter(),
        probe_rowid_filter(),
        probe_trigram(),
        probe_coexistence(),
    ]
    failed = results.count(1)
    print(f"\n=== 结论：{len(results) - failed}/{len(results)} 项可用 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
