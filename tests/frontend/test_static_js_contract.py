"""前端静态资源的静态契约：调用的名字必须在文件里声明过。

WHY 需要它：``interfaces/web/static/*.js`` 没有构建步骤、没有打包器，也就没有任何
「未定义引用」的检查。于是「改了个名字 / 删了个实现，漏改一处调用」这类缺陷只有真人
点开界面时才会暴露，而且现场表现往往是**误导性**的——本仓已经栽过两次：

- ``app.js`` 里三处 ``loadThread(...)``，实际函数叫 ``openThread(...)``（T16 分支收尾
  引入）。症状是重新生成与切换分支各弹一条「失败」，而后端日志里那些请求全是 200——
  操作其实成功了，只是界面没刷新，让人先怀疑后端；
- ``interfaces/web/app.py`` 里 ``_close_background_workers()``（OIDC 移除后残留）。
  那个能被 pytest 拦住（一个测试走到了 lifespan 的退出段），但它同时说明：**没有任何
  自动化手段在扫「引用了不存在的名字」**。

本文件把最朴素的那条规则钉进测试网。

WHY 用正则而不是 JS 解析器：引解析器等于给测试加一个 node 依赖与一份 AST 版本兼容面；
而这里要挡的是「名字压根不存在」这种粗粒度错误，文本层面的「声明集合 vs 调用集合」比对
已经足够。代价是必须有白名单——写小了会误报、写大了会漏报，因此下面两个白名单都显式
列出并逐条给出理由；新增条目时请按同一标准判断。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "interfaces" / "web" / "static"

_DECLARED = re.compile(
    r"\b(?:function|class)\s+(?P<fn>[A-Za-z_$][\w$]*)"
    r"|\b(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)"
)
"""函数 / 类声明与变量声明。赋值式定义（``const f = () => {}``）也在此列。"""

_PARAMS = re.compile(
    r"function\s*(?:[A-Za-z_$][\w$]*)?\s*\((?P<params>[^)]*)\)"
    r"|\((?P<arrow>[^)]*)\)\s*=>"
    r"|(?P<single>[A-Za-z_$][\w$]*)\s*=>"
)
"""形参。

WHY 必须认形参：``(function (root, factory) { ... })(this, factory)` 这种 UMD 外壳里
``factory`` 是被调用的，但它只是形参——不认它就会把正确代码判成违规，而一条会误报的
断言很快就没人看。
"""

_CALL = re.compile(r"(?<![\w$.])(?P<name>[A-Za-z_$][\w$]*)\s*\(")
"""裸调用：前面不是 ``.``（排除方法调用）、也不是标识符字符（排除 ``foo bar(``）。"""

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")
_REGEX_LITERAL = re.compile(
    r"(?m)(?:(?<=[(,=:\[!&|?{};])|(?<=return )|^)"
    r"/(?:\\.|\[(?:[^\]\\]|\\.)*\]|[^/\\\n])+/[a-z]*"
)
"""注释与正则字面量。

WHY 必须先剥掉这三类：在纯文本层面，它们与一次函数调用长得一模一样，实测三类都
误报过——
- 块注释里的 ``done(reason=stopped)``（``app.js`` 的 stopRun 说明）；
- 行注释里的 ``uuid4()``（``app.js`` 的会话 ID 形状说明）；
- 正则字面量 ``[\\s(]`` / ``[^_\\s]``（``markdown.js`` 的斜体规则）。

一条会把正确代码判成违规的断言，很快就会被当成噪音忽略——那比没有断言更糟。

WHY 正则字面量用「前一个字符决定它是不是正则」的启发式：JS 里 ``/`` 既可能是除法也
可能是正则开头，唯一可靠的判据是前文（``(``、``=``、``,``、``return``… 之后是正则，
标识符之后是除法）。本检查只在这些位置剥离，因此 ``a / b`` 不会被误当成正则吃掉。
"""


def _blank(match: re.Match[str]) -> str:
    """把匹配到的片段换成等长空白（保留换行），使行号不漂移。"""
    return re.sub(r"[^\n]", " ", match.group(0))


def _strip_noncode(source: str) -> str:
    """剥掉注释与正则字面量，只留会真正执行的文本。"""
    cleaned = _BLOCK_COMMENT.sub(_blank, source)
    cleaned = _REGEX_LITERAL.sub(_blank, cleaned)
    return _LINE_COMMENT.sub(_blank, cleaned)

_KEYWORDS = frozenset(
    {
        "if", "else", "for", "while", "do", "switch", "case", "default", "try", "catch",
        "finally", "return", "throw", "new", "typeof", "instanceof", "void", "delete",
        "in", "of", "yield", "await", "async", "function", "class", "super", "this",
        "with", "let", "const", "var", "import", "export",
    }
)
"""语句关键字：它们后面跟 ``(`` 是语法结构而不是函数调用。"""

_GLOBALS = frozenset(
    {
        # 语言内建
        "String", "Number", "Boolean", "BigInt", "Symbol", "Array", "Object", "JSON",
        "Math", "Date", "RegExp", "Error", "TypeError", "RangeError", "Promise", "Map",
        "Set", "WeakMap", "WeakSet", "Proxy", "Reflect",
        "parseInt", "parseFloat", "isNaN", "isFinite",
        "encodeURI", "decodeURI", "encodeURIComponent", "decodeURIComponent",
        # 浏览器 / 平台接口
        "fetch", "Headers", "Request", "Response", "URL", "URLSearchParams", "FormData",
        "Blob", "File", "FileReader", "TextEncoder", "TextDecoder",
        "AbortController", "AbortSignal", "WebSocket", "Event", "CustomEvent",
        "MutationObserver", "IntersectionObserver", "ResizeObserver", "Image",
        "setTimeout", "clearTimeout", "setInterval", "clearInterval",
        "requestAnimationFrame", "cancelAnimationFrame", "queueMicrotask", "structuredClone",
        "confirm", "alert", "prompt", "atob", "btoa", "getComputedStyle",
        "document", "window", "console", "navigator", "location", "history",
        "localStorage", "sessionStorage",
        # Node（node --test 直接加载这些文件时会用到）
        "require", "module", "exports", "define", "self", "globalThis",
    }
)
"""内建与平台全局：它们当然没有本地声明。

WHY 逐个列出而不是用 ``^[A-Z]`` 之类的形状规则：形状规则会把「大写开头就是内建」
写进断言，于是任何大小写风格不同的未定义调用都被放过——那恰好是最需要被挡住的。
"""


def _static_scripts() -> list[Path]:
    """全部前端脚本（按文件名排序，让失败信息稳定）。"""
    return sorted(STATIC_DIR.glob("*.js"))


def _declared_names(source: str) -> set[str]:
    """文件里声明过的名字集合（含形参）。"""
    names: set[str] = set()
    for match in _DECLARED.finditer(source):
        names.add(match.group("fn") or match.group("var"))
    for match in _PARAMS.finditer(source):
        raw = match.group("params") or match.group("arrow") or match.group("single") or ""
        for part in raw.split(","):
            candidate = part.strip().split("=")[0].strip().strip("{}[]")
            if re.fullmatch(r"[A-Za-z_$][\w$]*", candidate):
                names.add(candidate)
    return names


def test_static_scripts_are_discovered() -> None:
    """先确认 glob 命中：否则下面的断言会在「零个文件」上静默通过。"""
    names = {path.name for path in _static_scripts()}

    assert {
        "app.js",
        "markdown.js",
        "workspace_scope.js",
    } <= names, sorted(names)


def test_no_call_to_undeclared_identifier() -> None:
    """每个裸调用的名字都必须在本文件声明过（或在内建白名单里）。"""
    offenders: list[str] = []
    for path in _static_scripts():
        source = _strip_noncode(path.read_text(encoding="utf-8"))
        declared = _declared_names(source)
        for match in _CALL.finditer(source):
            name = match.group("name")
            if name in declared or name in _GLOBALS or name in _KEYWORDS:
                continue
            line = source[: match.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line} 调用了未声明的 {name}()")

    assert not offenders, (
        "这些调用指向了本文件里没有声明的名字（多半是改名或删除实现后漏改的调用点）：\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n\n若它确实是内建或形参，请把理由写进本文件的 _GLOBALS / _PARAMS，"
        "而不是把这条断言删掉。"
    )
