"""技术验证：SQLite FTS5 能否用于会话正文检索（T19 第六项，先出结论再实现）。

四个问题：
1. 本机 Python 自带的 SQLite 是否编入 FTS5，``trigram`` 分词器是否可用？
2. 中文召回如何——``unicode61`` 与 ``trigram`` 各自能匹配什么、匹配不了什么？
3. 索引体积：内容表与 contentless 表各占多少，相对原文是多少倍？
4. 查询性能，以及与检查点 BLOB 一致性的现实约束。

WHY 先测再实现：这一项的成本几乎全在「能不能检索中文」这个事实上——而它与 FTS5
的分词器行为、SQLite 版本、甚至编译选项有关，靠记忆推断必然出错。测出来的数字
直接决定「现在做」还是「不做、以及为什么不做」。
"""

from __future__ import annotations

import pathlib
import random
import sqlite3
import tempfile
import time

_CORPUS = [
    "帮我排查一下登录接口的超时问题，日志里只看到 p99 到了 3 秒",
    "数据库连接池的配置需要调整，max_overflow 太小了",
    "The quick brown fox jumps over the lazy dog",
    "帮我把 workspace 下的报告导出成 markdown",
    "幂等键应该由客户端生成，服务端只做校验",
]

_QUERIES = [
    ("登录接口", "中文四字"),
    ("超时", "中文两字"),
    ("连接池", "中文三字"),
    ("quick brown", "英文词组"),
    ("workspace", "英文单词"),
]


def _availability() -> bool:
    """问题 1：FTS5 与 trigram 是否可用。"""
    print("=== 问题 1：可用性 ===")
    print("sqlite 版本：", sqlite3.sqlite_version)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
    except sqlite3.OperationalError as exc:
        print("FTS5 不可用：", exc)
        connection.close()
        return False
    print("FTS5：可用")

    options = [row[0] for row in connection.execute("PRAGMA compile_options").fetchall()]
    print("编译选项含 ENABLE_FTS5：", any("FTS5" in item for item in options))

    try:
        connection.execute(
            "CREATE VIRTUAL TABLE probe2 USING fts5(body, tokenize='trigram')"
        )
        print("trigram 分词器：可用")
    except sqlite3.OperationalError as exc:
        print("trigram 分词器：不可用 →", exc)
    connection.close()
    return True


def _recall() -> None:
    """问题 2：两种分词器的中文召回。"""
    print("\n=== 问题 2：中文召回 ===")
    for tokenizer in ("unicode61", "trigram"):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            f"CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='{tokenizer}')"
        )
        for text in _CORPUS:
            connection.execute("INSERT INTO docs(body) VALUES (?)", (text,))
        print(f"--- tokenize={tokenizer} ---")
        for query, label in _QUERIES:
            try:
                hits = connection.execute(
                    "SELECT count(*) FROM docs WHERE docs MATCH ?", (f'"{query}"',)
                ).fetchone()[0]
                print(f"  {label:<8} 「{query}」→ 命中 {hits}")
            except sqlite3.OperationalError as exc:
                print(f"  {label:<8} 「{query}」→ 查询失败：{exc}")
        connection.close()


def _corpus(count: int = 5000) -> list[str]:
    """造一份长度分布接近真实消息的语料（中英混排）。"""
    random.seed(7)
    filler = "这是一段用于填充的说明文字，长度不一，模拟真实的消息正文。"
    rows: list[str] = []
    for index in range(count):
        base = f"[{index}] " + random.choice(_CORPUS) + " "
        rows.append(base + filler * random.randint(0, 3))
    return rows


def _size() -> None:
    """问题 3：索引体积。"""
    print("\n=== 问题 3：索引体积（5000 条消息）===")
    rows = _corpus()
    source_bytes = sum(len(row.encode("utf-8")) for row in rows)
    print(f"  原文合计：{source_bytes / 1024 / 1024:.2f} MB")

    workdir = pathlib.Path(tempfile.mkdtemp())
    for tokenizer in ("unicode61", "trigram"):
        for contentless in (False, True):
            label = "contentless" if contentless else "内容表"
            path = workdir / f"{tokenizer}-{label}.db"
            spec = "content=''" if contentless else ""
            connection = sqlite3.connect(path)
            # contentless 表不接受普通 INSERT，需要 contentless_delete/特殊语法；
            # 这里用 INSERT 到 'body' 列的方式写入（FTS5 支持 contentless 的写入）。
            connection.execute(
                f"CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='{tokenizer}'"
                + (f", {spec}" if spec else "")
                + ")"
            )
            try:
                connection.executemany(
                    "INSERT INTO docs(body) VALUES (?)", [(row,) for row in rows]
                )
                connection.commit()
            except sqlite3.OperationalError as exc:
                print(f"  {tokenizer:<9} {label:<11} 写入失败：{exc}")
                connection.close()
                continue
            size = path.stat().st_size
            print(
                f"  {tokenizer:<9} {label:<11} {size / 1024 / 1024:5.2f} MB"
                f"  （原文的 {size / source_bytes:.2f} 倍）"
            )
            connection.close()


def _performance() -> None:
    """问题 4：查询性能（与 LIKE 全表扫对比）。"""
    print("\n=== 问题 4：查询性能 ===")
    rows = _corpus()
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='trigram')")
    connection.execute("CREATE TABLE plain (body TEXT)")
    connection.executemany("INSERT INTO docs(body) VALUES (?)", [(row,) for row in rows])
    connection.executemany("INSERT INTO plain(body) VALUES (?)", [(row,) for row in rows])
    connection.commit()

    query = "登录接口"
    start = time.perf_counter()
    fts_hits = connection.execute(
        "SELECT count(*) FROM docs WHERE docs MATCH ?", (f'"{query}"',)
    ).fetchone()[0]
    fts_ms = (time.perf_counter() - start) * 1000

    start = time.perf_counter()
    like_hits = connection.execute(
        "SELECT count(*) FROM plain WHERE body LIKE ?", (f"%{query}%",)
    ).fetchone()[0]
    like_ms = (time.perf_counter() - start) * 1000

    print(f"  FTS5  MATCH ：{fts_ms:7.1f} ms，命中 {fts_hits}")
    print(f"  LIKE 全表扫 ：{like_ms:7.1f} ms，命中 {like_hits}")
    connection.close()


def _checkpoint_volume() -> None:
    """与检查点一致性相关的现实体量：索引只能由 Python 侧写入。"""
    print("\n=== 检查点侧的体量（决定回填成本）===")
    db_path = pathlib.Path(".data/agent.db")
    if not db_path.exists():
        print("  本机没有 .data/agent.db，跳过")
        return
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            try:
                count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                print(f"  {table:<18} {count:>6} 行")
            except sqlite3.OperationalError as exc:
                print(f"  {table:<18} 读取失败：{exc}")
        # 正文只存在 msgpack BLOB 里，索引必须由 Python 反序列化后写入
        print("  说明：消息正文在 checkpoints 的 msgpack BLOB 内，SQL 层无法直接索引。")
    finally:
        connection.close()


def main() -> int:
    """跑完四个问题并打印结论所需的原始数据。"""
    if not _availability():
        return 1
    _recall()
    _size()
    _performance()
    _checkpoint_volume()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
