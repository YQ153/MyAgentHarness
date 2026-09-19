"""``app.state`` 的装配契约：路由读到的每一项都必须在 lifespan 里铺开。

WHY 需要它：``app.state`` 是接口层与装配层之间的一处**隐式**契约——写的那一半在
``interfaces/web/app.py``，读的那一半散在各个路由里。少铺一项不会有任何报错，而是分两种
方式变成运行期故障：

- 读取方用 ``state.x``（严格）：真机上表现为 500 加一屏栈，看起来像「服务端有 bug」；
- 读取方用 ``getattr(state, "x", None)``（宽容）：**静默降级**，功能悄悄少一半。

本仓两种都发生过：``app.state.audit_store`` 漏铺，一边让审计查询端点 500，一边让
``auth/audit.py`` 把**全部认证审计**丢掉——后者藏了很久，直到有人点开审计面板才暴露。
本文件把「读到的必须被写过」钉成静态断言，正是为了不让第三例出现。

WHY 静态断言而不是跑一次真实应用：真应用的 lifespan 要建库、连模型、编译图，跑不起来就
无从验证；而这条契约本身是文本层面的（一处写、另一处读），静态比对就是它的准确形态。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = ROOT / "interfaces" / "web"
APP_FILE = WEB_DIR / "app.py"

_BLOCK_STRING = re.compile(r"\"\"\".*?\"\"\"|'''.*?'''", re.S)
_LINE_STRING = re.compile(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'")
_COMMENT = re.compile(r"#[^\n]*")

_STATE_ACCESS = re.compile(r"\.state\.(?P<name>[a-z_][\w]*)")
_ASSIGNMENT = re.compile(r"\.state\.(?P<name>[a-z_][\w]*)\s*=(?!=)")
_REQUIRE_STATE = re.compile(r"require_state\(\s*[^,]+,\s*\"(?P<name>[a-z_][\w]*)\"")

_MIN_READS = 5
"""期望至少扫到的读取项数。

WHY 要有个下限：正则一旦写错（例如漏掉了 ``request.app.state`` 这种形式），断言会在
「零个读取」上静默通过——那比没有断言更糟。
"""


def _blank(match: re.Match[str]) -> str:
    """把匹配片段换成等长空白（保留换行），让行号与后续匹配不漂移。"""
    return re.sub(r"[^\n]", " ", match.group(0))


def _strip_literals(source: str) -> str:
    """剥掉字符串与注释，只留真正的代码。

    WHY 必须剥：注释与日志文案里同样会出现 ``app.state.audit_store`` 这种字样（本仓
    刚为它写了说明），不剥就会把「解释这件事的句子」当成「读它的代码」。
    """
    stripped = _BLOCK_STRING.sub(_blank, source)
    stripped = _LINE_STRING.sub(_blank, stripped)
    return _COMMENT.sub(_blank, stripped)


def _reads() -> dict[str, list[str]]:
    """全部 ``app.state`` 读取点（名字 → 出现位置）。

    两种写法都算读取：严格取属性，以及 ``require_state(request, "name", ...)`` 这种把名字
    写成字符串的间接读取。后者若不算，恰好会漏掉本缺陷暴露的那一处（审计端点）——
    而它正是被 ``require_state`` 收口的。
    """
    found: dict[str, list[str]] = {}
    for path in sorted(WEB_DIR.rglob("*.py")):
        raw = path.read_text(encoding="utf-8")
        for match in _REQUIRE_STATE.finditer(raw):
            found.setdefault(match.group("name"), []).append(f"{path.name}（require_state）")
        source = _strip_literals(raw)
        for match in _STATE_ACCESS.finditer(source):
            if _ASSIGNMENT.match(source, match.start()):
                continue
            line = source[: match.start()].count("\n") + 1
            found.setdefault(match.group("name"), []).append(f"{path.name}:{line}")
    return found


def _assignments() -> set[str]:
    """``interfaces/web/app.py`` 里铺到 ``app.state`` 上的名字。"""
    source = _strip_literals(APP_FILE.read_text(encoding="utf-8"))
    return {match.group("name") for match in _ASSIGNMENT.finditer(source)}


def test_scanner_sees_both_sides() -> None:
    """先确认两侧都扫到了东西，否则下面的断言会在空集合上静默通过。"""
    reads = _reads()
    assignments = _assignments()

    assert len(reads) >= _MIN_READS, f"只扫到 {len(reads)} 个读取点，正则可能写错了：{sorted(reads)}"
    assert {"config", "api_key_store", "audit_store"} <= assignments, sorted(assignments)


def test_every_state_read_is_assigned_in_lifespan() -> None:
    """读到的每一项都必须在 ``app.py`` 里被写过。"""
    assignments = _assignments()
    missing = {
        name: where for name, where in _reads().items() if name not in assignments
    }

    assert not missing, (
        "这些 app.state 项被路由读取，但 interfaces/web/app.py 从未铺过"
        "（宽容读取会静默降级，严格读取会 500）：\n  "
        + "\n  ".join(f"app.state.{name} ← {', '.join(sorted(set(where)))}" for name, where in sorted(missing.items()))
    )
