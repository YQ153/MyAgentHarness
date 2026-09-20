"""`docs/overview/architecture.html` 的位置引用契约。

WHY 需要它：这份文档承诺「位置引用与源文件一致」。2026-09-20 逐条复核时的实测结果：
全篇 90 处引用里只有 3 处仍然正确，偏移 60~700 行不等；其中三处已指向**文件之外**
（``application/event_translator.py`` 只有 284 行，引用却写 ``:343-346``——那段代码在
「运行服务按职责拆为四块」时搬去了 ``run_service.py``）；另有三处引用的**内容**已不成立
（``llm/registry.py`` 反向依赖 ``agent.profiles``、``sandbox`` 档位抛
``NotImplementedError``、``interfaces`` 直接依赖 ``runtime``）。

修正只解决一次。没有防线的修正会立刻开始第二次腐烂，而读者按行号跳错地方时
不会有人知道——所以这里补上两条断言：

1. **存在性**：引用的文件存在，且行号在该文件长度内。拦的是「指向已删除的文件 /
   超出文件末尾」这类最粗的失效。
2. **符号一致**：引用紧邻的符号名（标识符形态）必须出现在它指向的行（或行区间）里。
   拦的是「符号搬家了、行号没跟」——上面 90 处里的绝大多数属于这一类。

裸行号（``:343-346`` 这种不带路径的写法）在 2026-09-20 已全部补成带路径形式：
它们的归属只能由读者从上下文推断，机器无从复核，而同一个裸号在 routes 表与 app.js
表里指的不是一回事。本文件对裸行号直接判失败——它逼作者写清归属，而不是留一个
谁也核不了的数字。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_DOC = ROOT / "docs/overview/architecture.html"

#: 带路径的引用：``<code>path/to/file.py:123</code>`` 或 ``<code>path/to/file.py:123-456</code>``
_REF = re.compile(r"<code>([A-Za-z0-9_./\-]+\.(?:py|js|toml|yml|md)):(\d+)(?:-(\d+))?</code>")

#: 裸行号：``<code>:123</code>``——归属无从判定，不允许再出现
_BARE_REF = re.compile(r"<code>:\d+(?:-\d+)?</code>")

_CODE_TOKEN = re.compile(r"<code>([^<]*)</code>")

#: 可当作"符号名"的形态：标识符，或 ``类名.方法名``
_SYMBOL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?(?:\(\))?$")

#: 相邻符号与引用之间不允许跨过单元格 / 列表项边界——跨过就不是"紧邻"了
_CELL_BOUNDARY = ("</td>", "</tr>", "<li>", "</li>", "</p>", "<br")


def _document() -> str:
    """读取文档正文。

    Raises:
        AssertionError: 文档不存在——路径写错时后续断言会因"无可检查"而全部通过。
    """
    assert _DOC.is_file(), f"找不到 {_DOC}"
    return _DOC.read_text(encoding="utf-8")


def _neighbouring_symbol(text: str, ref_start: int) -> str | None:
    """取出引用前"紧邻"的符号名；判定不出时返回 ``None``（该处只做存在性检查）。

    WHY 返回 ``None`` 而不是猜：文档里大量引用是"区域说明"（``:205-209`` 指一段告警、
    ``:475-482`` 指一次回落），它们旁边根本没有符号名。把这些也算成"待校验的符号"
    只会制造误报，而误报的防线很快会被绕开或删掉。

    Args:
        text: 文档正文。
        ref_start: 引用的起始下标。

    Returns:
        符号名（``类名.方法名`` 取方法名），或 ``None``。
    """
    gap_start = 0
    for boundary in _CELL_BOUNDARY:
        position = text.rfind(boundary, 0, ref_start)
        gap_start = max(gap_start, position + len(boundary))

    tokens = [m for m in _CODE_TOKEN.finditer(text, gap_start, ref_start)]
    if not tokens:
        return None
    token = tokens[-1].group(1).strip()
    matched = _SYMBOL.match(token)
    if matched is None:
        return None
    return matched.group(2) or matched.group(1)


def _references() -> list[dict[str, Any]]:
    """抽出全篇引用及其相邻符号。

    Returns:
        每项含 ``raw``（原文）、``path``、``start``、``end``、``symbol``。
    """
    text = _document()
    found: list[dict[str, Any]] = []
    for match in _REF.finditer(text):
        found.append(
            {
                "raw": match.group(0),
                "path": match.group(1),
                "start": int(match.group(2)),
                "end": int(match.group(3)) if match.group(3) else None,
                "symbol": _neighbouring_symbol(text, match.start()),
            }
        )
    return found


def test_document_references_are_extractable() -> None:
    """自检：抽不出引用说明正则失效了，后面的断言会变成空转。

    WHY 值得单独一条：这是唯一能发现"契约整体失效"的用例——若哪天文档改成了别的
    引用格式，其余用例会因为集合为空而全部通过，防线静默消失。
    """
    references = _references()

    assert len(references) >= 60, f"只抽到 {len(references)} 处引用，正则或文档格式可能已变"
    assert any(ref["symbol"] for ref in references), "没有任何引用能判定出相邻符号，符号一致性断言会空转"


def test_no_reference_is_left_unqualified() -> None:
    """裸行号必须为 0 处。

    WHY：不带路径的行号无法判定归属，也就无法校验——这正是本次复核里最费时的一类
    （同一个 ``:151`` 在 routes 表里指 ``POST /runs``，在 app.js 表里指 ``ensureThread``）。
    """
    text = _document()

    leftovers = _BARE_REF.findall(text)
    assert not leftovers, (
        f"文档里还有 {len(leftovers)} 处不带路径的行号：{leftovers[:8]}\n"
        "请写成 <code>相对路径:行号</code>——否则读者不知道它属于哪个文件，"
        "本契约也无从校验。"
    )


def test_every_reference_points_inside_its_file() -> None:
    """每个引用都指向存在的文件、且行号在文件长度内。"""
    problems: list[str] = []

    for ref in _references():
        target = ROOT / str(ref["path"])
        if not target.is_file():
            problems.append(f"{ref['raw']}：文件不存在")
            continue
        total = len(target.read_text(encoding="utf-8").splitlines())
        if ref["start"] > total:
            problems.append(f"{ref['raw']}：起始行超出文件长度（该文件 {total} 行）")
        if ref["end"] is not None and ref["end"] > total:
            problems.append(f"{ref['raw']}：结束行超出文件长度（该文件 {total} 行）")

    assert not problems, "以下引用指向了文件之外：\n" + "\n".join(f"  - {item}" for item in problems)


def test_referenced_line_contains_the_claimed_symbol() -> None:
    """引用声称的符号必须真的出现在它指向的行（或行区间）里。

    WHY 这是核心断言：行号漂移的典型形态是"符号搬了家、数字没跟"，
    而存在性检查对此毫无察觉——``main.py:79`` 曾经完全合法，只是那一行是
    ``logging.basicConfig(...)``，而文档说它是 ``main()``。
    """
    problems: list[str] = []

    for ref in _references():
        symbol = ref["symbol"]
        if not symbol:
            continue
        target = ROOT / str(ref["path"])
        if not target.is_file():
            continue  # 存在性由另一条断言负责，这里只谈符号
        lines = target.read_text(encoding="utf-8").splitlines()
        end = ref["end"] or ref["start"]
        window = lines[ref["start"] - 1 : min(end, len(lines))]
        if not any(symbol in line for line in window):
            head = window[0].strip()[:70] if window else "(超出行范围)"
            problems.append(
                f"{ref['raw']} 声称是 {symbol}，但该行是：{head}"
            )

    assert not problems, (
        "以下引用的行号与它声称的符号对不上（符号多半搬过家）：\n"
        + "\n".join(f"  - {item}" for item in problems)
        + "\n若该引用本就指向一段区域或说明，请把符号写成紧邻的前一个 <code>，"
        "或让它指向包含该符号定义的那一行。"
    )
