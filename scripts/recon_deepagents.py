"""一次性侦查 deepagents 内部结构（M0 方案制定用，用后可删）。

产出：
1. 包内模块清单与行数；
2. graph.py 的类/函数布局与 create_deep_agent 的 middleware 组装段；
3. backends/protocol.py 的协议方法签名；
4. 各 middleware 的类名与关键钩子（before_agent / wrap_model_call / wrap_tool_call / tools）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PKG = Path(".venv/Lib/site-packages/deepagents")
OUT = Path("docs/deepagents_recon.txt")


def module_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def outline(path: Path) -> list[str]:
    out: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(f"  L{node.lineno}: {type(node).__name__} {node.name}")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(f"    L{sub.lineno}:   def {sub.name}")
    return out


def main() -> None:
    lines: list[str] = ["=== 包结构 ==="]
    for py in sorted(PKG.rglob("*.py")):
        rel = py.relative_to(PKG)
        n = len(module_lines(py))
        lines.append(f"{rel}  ({n} lines)")

    graph = PKG / "graph.py"
    lines += ["", "=== graph.py 布局 ===", *outline(graph)]

    body = "\n".join(module_lines(graph))
    # 抽 create_deep_agent 里 middleware 组装相关行（含 8 空格缩进的关键词行）
    lines += ["", "=== graph.py 中 middleware 相关片段 ==="]
    for i, line in enumerate(module_lines(graph), 1):
        if re.search(r"Middleware\(|middleware\.append|base_stack|fs_middleware|skills", line):
            lines.append(f"  L{i}: {line.rstrip()}")

    proto = PKG / "backends" / "protocol.py"
    if proto.exists():
        lines += ["", "=== backends/protocol.py 全文 ===", *module_lines(proto)]

    mw = PKG / "middleware"
    if mw.exists():
        for py in sorted(mw.rglob("*.py")):
            rel = py.relative_to(PKG)
            lines += ["", f"=== {rel} 布局 ===", *outline(py)]

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写出 {OUT}（{len(lines)} 行）")


if __name__ == "__main__":
    main()
