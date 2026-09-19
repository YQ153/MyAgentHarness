"""会话与知识库的运行期取证：某个会话到底调用了哪些工具，答案有没有走知识库。

WHY 需要这个脚本，而不是走 REST：``GET /api/threads/{id}/export`` 与
``GET /auth/audit`` 确实能回答「调了哪些工具」，但它们要求凭据可用、服务在运行，
而且拿不到 ``invalid_tool_calls``、``reasoning_content`` 这类字段——判定「模型只是
在思考里提到了某个工具名」还是「真的发起了调用」恰好依赖这些字段。排障时最常见
的三种处境（服务起不来、会话已被真删除、库正在被写）REST 都覆盖不到。

WHY 读副本而不是直接连库：应用进程持有 WAL 连接，直连会与它抢锁，也可能读到撕裂
的中间状态。先复制 ``agent.db`` 及其 ``-wal`` / ``-shm`` 再读，天然是「某一瞬间的
一致视图」，且对线上进程零打扰——这也是本脚本不需要任何并发控制的原因。

WHY 只输出结构性事实（角色 / 工具名 / 长度）：本脚本要判定的是「答案来自检索还是
模型直接生成」，这只需要工具调用链，不需要对话正文；不打印正文的同时也让输出可以
安全地贴进工单或群聊。

用法::

    python scripts/inspect_thread.py thread 32a0f19c5b314b92af9149bb4e18ae29
    python scripts/inspect_thread.py thread 32a0f19c... --explain
    python scripts/inspect_thread.py knowledge

退出码：``0`` 成功；``1`` 数据库不存在或无法读取；``2`` 参数不合法；
``3`` 指定会话在库中不存在（此时不做任何判定——零行不等于「没有工具调用」）。
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
"""仓库根；``AppConfig`` 的相对路径按 CWD 解析，配置加载失败时用它兜底。"""

sys.path.insert(0, str(ROOT))
# WHY 强制 UTF-8：输出全是中文，Windows 控制台默认 GBK 会让 print 抛
# UnicodeEncodeError——那会把一次成功的取证变成一条与本意无关的编码错误。
# stderr 一并处理：日志走 stderr，只改 stdout 会让「取证结束」这类日志变成乱码。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_TITLE_WIDTH = 18

_CANDIDATE_TOOLS = (
    "search_documents",
    "index_documents",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "execute",
    "write_todos",
    "task",
    "web_search",
    "web_fetch",
)
"""工具名候选。

WHY 需要它：检查点是 msgpack，上游改一次消息结构就可能让逐字段解码失败。字节扫描
不依赖任何结构，代价是它分不清「正文里提到」与「真的调用」——所以它只作为兜底，
并在 ``--explain`` 里再逐字段定位一次。
"""

_EXPLAIN_FIELDS = (
    "content",
    "additional_kwargs",
    "response_metadata",
    "tool_calls",
    "invalid_tool_calls",
    "usage_metadata",
    "name",
    "id",
)


# --------------------------------------------------------------------- 基础设施


_KNOWLEDGE_DB_NAME = "knowledge.db"
"""知识库文件名的兜底取值，与 ``knowledge_runtime.KNOWLEDGE_DB_NAME`` 同值。

WHY 允许这份重复：正常路径取 ``knowledge_runtime.KNOWLEDGE_DB_NAME``（见
``knowledge_db_name``），只有在该模块导入不了时才用它。而导入不了恰恰是排障的典型
场景——``knowledge_runtime`` 会连带拉起 ``application`` → ``agent.graph`` →
``deepagents``，依赖没装全时这个脚本必须在**没有**它们的情况下也能跑，否则它
最需要的时候正好用不了。
"""

_DEFAULT_DATA_DIR = ".data"
"""配置也加载不了时的兜底数据目录（``AppConfig`` 的 ``DB_PATH`` 默认值所在处）。"""


def resolve_agent_db(db_override: str | None) -> Path:
    """解析 ``agent.db`` 路径。

    WHY 走 ``AppConfig`` 而不是写死 ``.data/``：``DB_PATH`` 是可配置的，容器部署下
    库在挂载卷里；写死会让「脚本查了另一个库」这种错误看起来像「数据不存在」。

    WHY 配置加载失败要兜底而不是直接退出：配置坏了恰恰是最需要取证的时刻，此时读库
    的诉求并没有消失。兜底到仓库根的默认路径，并留下 WARNING——降级可以被看见，
    才不会变成「脚本查的是另一个库，而没人知道」。

    Args:
        db_override: 显式指定的主库路径；``None`` 表示取配置。
    """
    if db_override:
        return Path(db_override).expanduser().resolve()

    from config import AppConfig  # noqa: PLC0415 - 仅本分支需要，避免脚本启动即拉起整个配置栈

    try:
        return Path(AppConfig().db_path)
    except Exception as exc:  # noqa: BLE001 - 配置异常类型很多，取证脚本要能继续
        logger.warning("配置加载失败，回退到默认路径：%s: %s", type(exc).__name__, exc)
        return ROOT / _DEFAULT_DATA_DIR / "agent.db"


def knowledge_db_name() -> str:
    """知识库文件名：优先取 ``knowledge_runtime`` 的常量，导入不了时用兜底值。

    WHY 允许兜底：该模块会连带拉起 ``application`` → ``agent.graph`` → ``deepagents``，
    依赖没装全时这条 import 会失败——而那时正是最需要这个脚本的时候。
    """
    try:
        from knowledge_runtime import KNOWLEDGE_DB_NAME  # noqa: PLC0415 - 按需导入
    except ImportError as exc:
        logger.warning("knowledge_runtime 导入失败，用兜底文件名：%s", exc)
        return _KNOWLEDGE_DB_NAME
    return KNOWLEDGE_DB_NAME


def resolve_knowledge_db(agent_db: Path) -> Path:
    """由主库路径推导知识库路径。

    WHY 跟着主库推导：知识库与检查点库同处一个数据目录是一等约定（``knowledge_runtime``
    的 docstring 写明），容器部署下两者同在一个可写卷里。
    """
    return agent_db.parent / knowledge_db_name()


def snapshot(source: Path, target_dir: Path) -> Path:
    """把库及其 WAL/SHM 复制进 ``target_dir``，返回副本路径。

    WHY 三个文件都要复制：WAL 模式下最近写入可能还只在 ``-wal`` 里，只复制主库会
    得到一份「看起来正常但少了最新几轮」的数据——那种缺失最难被察觉。

    WHY 副本目录由调用方给：由调用方用 ``TemporaryDirectory`` 持有，退出时连同
    副本一起回收；在这里自己 ``mkdtemp`` 会留下一个没人清理的目录。

    Raises:
        FileNotFoundError: 源库不存在。
    """
    if not source.is_file():
        raise FileNotFoundError(f"数据库不存在：{source}")

    for suffix in ("", "-wal", "-shm"):
        candidate = source.with_name(source.name + suffix)
        if candidate.exists():
            shutil.copy2(candidate, target_dir / (source.name + suffix))
    logger.debug("已生成快照：%s", target_dir)
    return target_dir / source.name


def connect(copy: Path) -> sqlite3.Connection:
    """以只读意图打开副本。

    WHY 仍然用常规连接而不是 ``mode=ro`` URI：副本归本脚本独占，不需要只读保护；
    而 ``mode=ro`` 在 ``-shm`` 缺失时反而会打不开——那正是「应用刚被 SIGKILL」时
    库的样子，也是排障会遇到的形态。
    """
    connection = sqlite3.connect(str(copy))
    connection.row_factory = sqlite3.Row
    return connection


def tables_of(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]


def columns_of(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info('{table}')")]


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def decode(blob: Any, kind: str) -> Any:
    """用 langgraph 自己的序列化器解开检查点/写入，保留消息对象结构。

    Raises:
        Exception: 上游结构变化或类型未注册时原样抛出，由调用方决定如何兜底——
            在这里吞掉会让「解码不了」表现为「这个会话没有消息」。
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: PLC0415

    return JsonPlusSerializer().loads_typed((kind, blob))


def as_text(value: object) -> str:
    """把任意字段渲染成可比较长度的文本（用于定位工具名字节，不用于展示）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, int, float, bool)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def byte_scan(blob: Any, label: str) -> list[str]:
    """兜底：直接在字节里找工具名，返回命中的名字。

    WHY 结果必须与结构化提取分开统计：字节命中既可能是真调用，也可能只是系统提示、
    记忆文件或模型思考里**提到**了这个名字。把它和 ``tool_calls`` 混在一张清单里
    会直接给出相反结论——所以这里打印时标注「弱证据」，并由调用方单独归类。
    """
    data = blob if isinstance(blob, bytes) else str(blob).encode("utf-8")
    hits = [name for name in _CANDIDATE_TOOLS if name.encode("utf-8") in data]
    if hits:
        print(f"    {label} 字节扫描命中（弱证据，可能是正文/提示里提到）：{', '.join(hits)}")
    return hits


# --------------------------------------------------------------------- thread


def show_meta(connection: sqlite3.Connection, thread: str) -> None:
    section("[1] 会话元数据 thread_meta（标题只报长度）")
    rows = list(
        connection.execute(
            "SELECT * FROM thread_meta WHERE thread_id LIKE ?", (thread + "%",)
        )
    )
    if not rows:
        print(f"  未找到 thread_id LIKE {thread}% 的记录（会话可能已被真删除）")
        return
    for row in rows:
        for key in row.keys():
            if key == "title":
                print(f"  {key:<{_TITLE_WIDTH}}[未打印：{len(str(row[key]))} 字符]")
            else:
                print(f"  {key:<{_TITLE_WIDTH}}{row[key]}")


def show_branches(connection: sqlite3.Connection, thread: str) -> None:
    section("[2] 分支 thread_branches")
    rows = list(
        connection.execute(
            "SELECT * FROM thread_branches WHERE thread_id LIKE ? ORDER BY branch_id",
            (thread + "%",),
        )
    )
    print(f"  {len(rows)} 条分支登记")
    for row in rows:
        print("  " + " | ".join(f"{key}={row[key]}" for key in row.keys()))


def show_audit(connection: sqlite3.Connection, thread: str) -> None:
    section("[3] 审计 audit_log（扩展工具的调用默认落在这里）")
    rows = list(
        connection.execute(
            "SELECT id, created_at, event_type, actor_id, target_id, action, outcome, details "
            "FROM audit_log WHERE target_id LIKE ? ORDER BY id",
            (thread + "%",),
        )
    )
    print(f"  命中 {len(rows)} 条")
    if not rows:
        print("  提示：没有任何记录说明该会话没有被跑到，或审计未落库")
    for row in rows:
        print(
            f"  #{row['id']} {row['created_at']} {row['event_type']} "
            f"action={row['action']} outcome={row['outcome']} actor={row['actor_id']}"
        )
        if row["details"]:
            print(f"      details={row['details']}")
    if not any(row["event_type"] == "tool_call" for row in rows):
        print("  注：无 tool_call 事件。内置工具默认不落审计，只有扩展工具（含知识库工具）才记")


def show_usage(connection: sqlite3.Connection, thread: str) -> None:
    section("[4] 用量 usage_log")
    rows = list(
        connection.execute(
            "SELECT * FROM usage_log WHERE thread_id LIKE ? ORDER BY id", (thread + "%",)
        )
    )
    print(f"  命中 {len(rows)} 行")
    if not rows:
        print("  注：没有用量行说明没有模型调用发生（例如运行被限流或直接失败）")
    for row in rows:
        print("  " + " | ".join(f"{key}={row[key]}" for key in row.keys()))


def describe_messages(messages: list[Any]) -> list[str]:
    """打印每条消息的角色/工具名/正文长度，返回本批消息里的工具名。"""
    calls: list[str] = []
    for index, message in enumerate(messages, start=1):
        role = getattr(message, "type", None) or getattr(message, "role", "?")
        name = getattr(message, "name", "") or ""
        content = as_text(getattr(message, "content", ""))
        tool_calls = getattr(message, "tool_calls", None) or []
        names = [
            str(call.get("name"))
            for call in tool_calls
            if isinstance(call, dict) and call.get("name")
        ]
        invalid = getattr(message, "invalid_tool_calls", None) or []
        suffix = f" 工具调用={names}" if names else ""
        invalid_note = f" 非法工具调用={len(invalid)}" if invalid else ""
        print(
            f"    {index:>3}. role={role}{f'/name={name}' if name else ''}"
            f" 正文长度={len(content)}{suffix}{invalid_note}"
        )
        calls.extend(names)
    return calls


def show_checkpoints(connection: sqlite3.Connection, thread: str) -> tuple[list[str], list[str]]:
    """检查点取证。

    Returns:
        ``(结构化工具调用, 字节扫描命中)``——两者分开返回，判定只用前者。
    """
    section("[5] 检查点 checkpoints")
    rows = list(
        connection.execute(
            "SELECT rowid AS rid, checkpoint_id, type, checkpoint FROM checkpoints "
            "WHERE thread_id LIKE ? ORDER BY rowid",
            (thread + "%",),
        )
    )
    print(f"  共 {len(rows)} 个检查点")
    calls: list[str] = []
    scans: list[str] = []
    for row in rows:
        print(f"\n  --- rowid={row['rid']} checkpoint_id={row['checkpoint_id']} ---")
        try:
            payload = decode(row["checkpoint"], row["type"])
        except Exception as exc:  # noqa: BLE001 - 解码失败要如实报出并继续下一行
            logger.warning("检查点解码失败（rowid=%s）：%s", row["rid"], exc)
            print(f"    [!] 解码失败（{type(exc).__name__}），改用字节扫描")
            scans.extend(byte_scan(row["checkpoint"], "checkpoint"))
            continue

        channel_values = payload.get("channel_values") or {}
        summary = ", ".join(
            f"{key}({type(value).__name__}"
            f"{f', {len(value)} 项' if isinstance(value, (list, dict)) else ''})"
            for key, value in channel_values.items()
        )
        print(f"    通道：{summary or '（空）'}")
        messages = channel_values.get("messages")
        if isinstance(messages, list) and messages:
            calls.extend(describe_messages(messages))
        scans.extend(byte_scan(row["checkpoint"], "checkpoint"))
    return calls, scans


def show_writes(connection: sqlite3.Connection, thread: str) -> tuple[list[str], list[str]]:
    """待写入取证；返回口径同 ``show_checkpoints``。"""
    section("[6] 待写入 writes（消息常落在这里）")
    rows = list(
        connection.execute(
            "SELECT rowid AS rid, checkpoint_id, task_id, idx, channel, type, value "
            "FROM writes WHERE thread_id LIKE ? ORDER BY rowid",
            (thread + "%",),
        )
    )
    print(f"  共 {len(rows)} 条写入")
    calls: list[str] = []
    scans: list[str] = []
    for row in rows:
        print(
            f"\n  --- rowid={row['rid']} channel={row['channel']} "
            f"task={row['task_id']} idx={row['idx']} ---"
        )
        try:
            value = decode(row["value"], row["type"])
        except Exception as exc:  # noqa: BLE001 - 同上
            logger.warning("写入解码失败（rowid=%s）：%s", row["rid"], exc)
            print(f"    [!] 解码失败（{type(exc).__name__}），改用字节扫描")
            scans.extend(byte_scan(row["value"], "write"))
            continue

        if isinstance(value, list):
            calls.extend(describe_messages(value))
        else:
            print(f"    值类型：{type(value).__name__}")
        scans.extend(byte_scan(row["value"], "write"))
    return calls, scans


def explain_tool_bytes(connection: sqlite3.Connection, thread: str) -> None:
    """逐字段定位工具名字节出现在哪个字段。

    WHY 必须单独做：整块字节扫描会把「正文/思考里提到某工具名」与「真的产生了工具
    调用」混为一谈，而这两者指向完全相反的结论。这里逐字段判断，只输出字段名与
    布尔值。
    """
    section("[7] 逐字段定位（--explain）")
    rows = list(
        connection.execute(
            "SELECT rowid AS rid, type, value FROM writes "
            "WHERE thread_id LIKE ? AND channel = 'messages' ORDER BY rowid",
            (thread + "%",),
        )
    )
    print(f"  messages 通道的写入共 {len(rows)} 条")
    for row in rows:
        try:
            messages = decode(row["value"], row["type"])
        except Exception as exc:  # noqa: BLE001 - 同上
            logger.warning("消息解码失败（rowid=%s）：%s", row["rid"], exc)
            print(f"  --- rowid={row['rid']} [!] 解码失败（{type(exc).__name__}）")
            continue

        for message in messages:
            role = getattr(message, "type", type(message).__name__)
            print(f"\n  --- rowid={row['rid']} role={role} 类={type(message).__name__} ---")
            for field in _EXPLAIN_FIELDS:
                if not hasattr(message, field):
                    continue
                raw = getattr(message, field)
                text = as_text(raw)
                hits = [name for name in _CANDIDATE_TOOLS if name in text]
                note = f"  ← 含 {hits}" if hits else ""
                print(f"    {field:<20} 长度={len(text):<8}{note}")
                if isinstance(raw, dict):
                    print(f"      └ 字段名：{sorted(raw.keys())}")
                    for key, item in raw.items():
                        key_hits = [
                            name for name in _CANDIDATE_TOOLS if name in as_text(item)
                        ]
                        if key_hits:
                            print(f"      └ 键 {key!r} 内含 {key_hits}（值不打印）")

            calls = getattr(message, "tool_calls", None) or []
            print(f"    tool_calls 条数={len(calls)}")
            for call in calls:
                if not isinstance(call, dict):
                    continue
                args = call.get("args")
                arg_keys: Any = sorted(args.keys()) if isinstance(args, dict) else type(args).__name__
                print(
                    f"      - name={call.get('name')} id={call.get('id')} "
                    f"args 字段={arg_keys}（值不打印）"
                )


def thread_exists(connection: sqlite3.Connection, thread: str, tables: list[str]) -> bool:
    """会话在库里是否有任何痕迹（元数据、检查点、写入三者任一）。

    WHY 必须先判有无：会话不存在时所有查询都返回零行，而「零行」会被判定逻辑读成
    「这个会话没有任何工具调用」——那是**与事实相反**的结论。排查脚本给出反向结论
    比不给出结论更糟。
    """
    for table in ("thread_meta", "checkpoints", "writes"):
        if table not in tables:
            continue
        found = connection.execute(
            f"SELECT 1 FROM {table} WHERE thread_id LIKE ? LIMIT 1", (thread + "%",)
        ).fetchone()
        if found:
            return True
    return False


def command_thread(
    connection: sqlite3.Connection, thread: str, *, explain: bool, tables: list[str]
) -> int:
    """会话取证主流程，返回退出码。"""
    if not thread_exists(connection, thread, tables):
        section("[中止]")
        print(f"  库中不存在会话 {thread}：元数据、检查点、写入三处都没有它的痕迹")
        print("  可能原因：id 有误、会话已被真删除、或指向了另一个数据目录（用 --db 指定）")
        return 3

    show_meta(connection, thread)
    show_branches(connection, thread)
    show_audit(connection, thread)
    show_usage(connection, thread)
    calls, scans = show_checkpoints(connection, thread)
    write_calls, write_scans = show_writes(connection, thread)
    calls.extend(write_calls)
    scans.extend(write_scans)
    if explain:
        explain_tool_bytes(connection, thread)

    section("[判定]")
    seen = list(dict.fromkeys(calls))
    weak = sorted({name for name in scans if name not in seen})
    print(f"  结构化工具调用（来自消息的 tool_calls）：{seen or '（无）'}")
    if weak:
        print(f"  字节扫描命中（弱证据，可能是系统提示/记忆/思考里提到）：{weak}")
    if "search_documents" in seen:
        print("  >>> 调用了 search_documents：答案有走知识库检索")
    elif "index_documents" in seen:
        print("  >>> 只调用了 index_documents（刷新索引）：答案不是来自知识库")
    elif seen:
        print("  >>> 只用到内置/其他工具，没有 search_documents：答案不是来自知识库检索")
    elif weak:
        print(
            "  >>> 没有任何结构化工具调用：答案由模型直接生成；"
            "上面的字节命中不是调用，用 --explain 可定位它落在哪个字段"
        )
    else:
        print("  >>> 一条工具调用都没有：答案由模型直接生成")
    print(
        "  注：工具名候选由本脚本内置（见 _CANDIDATE_TOOLS）；若上游改了工具名，"
        "以消息列表里的「工具调用=」为准"
    )
    return 0


# ------------------------------------------------------------------ knowledge


def command_knowledge(connection: sqlite3.Connection, label: str) -> int:
    """知识库概览：规模与已索引文档（在已打开的连接上跑，复用同一条快照链路）。"""
    section("[知识库] knowledge.db")
    names = tables_of(connection)
    print(f"  库文件：{label}")
    print(f"  表：{names}")

    for table in ("knowledge_meta", "knowledge_documents", "knowledge_chunks"):
        if table not in names:
            print(f"  {table}：表不存在")
            continue
        if table == "knowledge_meta":
            for row in connection.execute(f"SELECT * FROM {table}"):
                print(
                    "  knowledge_meta："
                    + " | ".join(f"{key}={row[key]}" for key in row.keys())
                )
            continue
        count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table}：{count} 行")

    if "knowledge_documents" not in names:
        return 0

    cols = columns_of(connection, "knowledge_documents")
    rows = list(
        connection.execute(
            f"SELECT {', '.join(cols)} FROM knowledge_documents "
            "ORDER BY indexed_at DESC LIMIT 50"
        )
    )
    print(f"\n  最近 {len(rows)} 份文档（最多 50）：")
    for row in rows:
        print("  " + " | ".join(str(row[key]) for key in row.keys()))

    owners = list(
        connection.execute(
            "SELECT owner_id, count(*) AS docs FROM knowledge_documents "
            "GROUP BY owner_id ORDER BY docs DESC"
        )
    )
    print("\n  按主体分布：")
    for row in owners:
        print(f"    owner={row['owner_id'] or '(空)'} 文档={row['docs']}")
    print(
        "  提示：/_tool_outputs/ 开头的路径是工具输出留存被索引进来的结果；"
        "若它出现在清单里，说明这些旁路留存正在参与检索"
    )
    return 0


# ------------------------------------------------------------------------ 入口


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="会话与知识库的运行期取证（只读副本，不打扰运行中的服务）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    thread_parser = subparsers.add_parser("thread", help="取证单个会话：消息构成与工具调用")
    thread_parser.add_argument("thread_id", help="会话标识")
    thread_parser.add_argument(
        "--explain",
        action="store_true",
        help="逐字段定位工具名字节出现在哪个字段（区分「提到」与「调用」）",
    )
    thread_parser.add_argument("--db", default=None, help="覆盖 agent.db 路径（默认取配置）")

    knowledge_parser = subparsers.add_parser("knowledge", help="知识库规模与已索引文档")
    knowledge_parser.add_argument("--db", default=None, help="覆盖 agent.db 路径（默认取配置）")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    args = parse_args(argv)

    from thread_utils import normalize_thread_id  # noqa: PLC0415 - 与运行时同一份口径

    if args.command == "thread":
        try:
            thread = normalize_thread_id(args.thread_id)
        except ValueError as exc:
            logger.error("会话标识不合法：%s", exc)
            print(f"会话标识不合法：{exc}")
            return 2
    else:
        thread = ""

    agent_db = resolve_agent_db(args.db)
    target = resolve_knowledge_db(agent_db) if args.command == "knowledge" else agent_db
    print(f"目标库：{target}")
    logger.info("取证开始：command=%s target=%s", args.command, target)

    try:
        with tempfile.TemporaryDirectory(prefix="mah-inspect-") as workdir:
            copy = snapshot(target, Path(workdir))
            connection = connect(copy)
            try:
                if args.command == "knowledge":
                    return command_knowledge(connection, copy.name)
                print(f"目标会话：{thread}")
                return command_thread(
                    connection, thread, explain=args.explain, tables=tables_of(connection)
                )
            finally:
                connection.close()
    except FileNotFoundError as exc:
        # WHY 单独区分：库不存在与库坏了是两种处置（前者多半是跑错目录或选错路径）
        logger.error("目标库不可用：%s", exc)
        print(f"\n[!] {exc}")
        print("    提示：在仓库根运行本脚本，或用 --db 指定路径")
        return 1
    except sqlite3.Error as exc:
        logger.exception("读取失败")
        print(f"\n[!] 读取失败：{type(exc).__name__}: {exc}")
        return 1
    finally:
        logger.info("取证结束：command=%s", args.command)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
