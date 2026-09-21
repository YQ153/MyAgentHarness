"""工具输出留存的回归测试。

覆盖面：文件名清洗（工具名可能来自 MCP，含各种字符）、序号定宽（字典序＝时间序）、
超长留存截断、按上限清理旧文件。
"""

from __future__ import annotations

from pathlib import Path

from runtime.tool_outputs import (
    prune_tool_outputs,
    sanitize_tool_name,
    tool_output_path,
    tool_output_virtual_path,
    write_tool_output,
)


# ------------------------------------------------------------------ 文件名清洗


def test_sanitize_keeps_safe_characters():
    assert sanitize_tool_name("web_fetch") == "web_fetch"
    assert sanitize_tool_name("srv-1_weather.v2") == "srv-1_weather.v2"


def test_sanitize_replaces_path_separators_and_spaces():
    """工具名来自工具注册表（含 MCP server 提供的名字），带 ``/`` 会建出多层目录。"""
    assert sanitize_tool_name("mcp/weather") == "mcp_weather"
    assert sanitize_tool_name("my tool") == "my_tool"
    # 非 ASCII 段被压成下划线，首尾的 ``._`` 再被剥掉（避免生成 "."／".." 这类危险名）
    assert sanitize_tool_name("偏好.md") == "md"


def test_sanitize_falls_back_when_names_are_unusable():
    assert sanitize_tool_name("") == "tool"
    assert sanitize_tool_name("...") == "tool"
    assert sanitize_tool_name("/") == "tool"


def test_sanitize_caps_length():
    assert len(sanitize_tool_name("x" * 200)) == 40


# ------------------------------------------------------------------ 路径与写盘


def test_output_path_layout(tmp_path: Path):
    """留存的形状：``<存储目录>/<会话 ID>/<NNNN>-<工具>.txt``。

    WHY 存储目录现在由调用方给（``store_dir``）：留存已经搬出工作区、落在根外存储里，
    「放在哪」由 ``config`` 决定；本模块只管「会话分子目录 + 定宽序号 + 清洗过的工具名」。
    """
    store = tmp_path / "tool-outputs"

    path = tool_output_path(store, "abc123", 1, "execute")

    assert path == store / "abc123" / "0001-execute.txt"
    # 序号定宽零填充 → 文件名的字典序等于时间序，清理时无需解析时间
    assert tool_output_path(store, "abc123", 10, "execute").name > path.name


def test_virtual_path_stays_in_sync_with_the_host_path(tmp_path: Path):
    """落盘路径与「消息里那个引用」必须同源。

    WHY 单列：这是本条链路上最容易错、又最难从现场看出来的地方——落盘成功、引用也生成了，
    只是两者指向不同位置；表现是前端点开「完整输出」时拿到 404，看起来像留存没写成功。
    """
    store = tmp_path / "tool-outputs"

    path = tool_output_path(store, "abc123", 1, "execute")

    assert tool_output_virtual_path("/_tool_outputs", "abc123", path.name) == (
        "/_tool_outputs/abc123/0001-execute.txt"
    )
    # 两端必须用同一套会话 ID 清洗规则，否则目录段对不上
    assert tool_output_virtual_path("/_tool_outputs", "my thread/1", "x.txt") == (
        "/_tool_outputs/my_thread_1/x.txt"
    )


def test_write_creates_directories_and_utf8(tmp_path: Path):
    path = tool_output_path(tmp_path / "store", "t1", 1, "read_file")

    write_tool_output(path, "中文内容\n", max_chars=1000)

    assert path.read_text(encoding="utf-8") == "中文内容\n"


def test_write_preserves_bytes_verbatim(tmp_path: Path):
    """换行不得被改写。

    WHY 钉住这条：文本模式在 Windows 上会把 ``\\n`` 转成 ``\\r\\n``，于是「留存完整
    输出」留的是一份**被改写过的**副本（实测一次 22,441 字符的输出多出 801 字节），
    而回取时通用换行又会把它折回成另一个形状。留存的价值在于逐字节原样。"""
    path = tmp_path / "raw.txt"

    write_tool_output(path, "a\nb\n", max_chars=1000)

    assert path.read_bytes() == b"a\nb\n"


def test_write_caps_oversized_output(tmp_path: Path):
    path = tmp_path / "big.txt"

    write_tool_output(path, "x" * 5000, max_chars=100)

    saved = path.read_text(encoding="utf-8")
    assert saved.startswith("x" * 100)
    # 截断要显式标注：否则「留存副本」看起来就是完整结果
    assert "truncated at 100 chars" in saved


# ------------------------------------------------------------------ 清理


def test_prune_keeps_newest_files(tmp_path: Path):
    for index in range(1, 6):
        write_tool_output(tmp_path / f"{index:04d}-t.txt", f"内容{index}", max_chars=100)

    removed = prune_tool_outputs(tmp_path, keep=2)

    assert removed == 3
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "0004-t.txt",
        "0005-t.txt",
    ]


def test_prune_is_noop_for_missing_dir_or_zero_keep(tmp_path: Path):
    assert prune_tool_outputs(tmp_path / "nope", keep=5) == 0
    assert prune_tool_outputs(tmp_path, keep=0) == 0
