"""仓库内导入关系的静态解析，供各层依赖契约测试共用。

WHY 独立成模块：``tests/test_root_module_contract.py``（根级模块角色表）与
``tests/application/test_layer_purity_contract.py``（应用层运行时端口与层内纯度）
都需要"读源码 → 解析 import"，且都必须遵守同一条纪律——**读取失败或语法错误
必须显式失败**。静默返回空集会让所有断言"通过"，契约整体空转，而这类失效
没有任何征兆（没有任何测试变红）。

两者共享的是这段基础设施，而不是各自的判据：前者的判据是"顶层模块名集合"，
后者的判据是"包内模块与具体符号"。判据不共享是有意的，基础设施共享也是有意
的——否则同一段解析逻辑会有两份，其中一份的修复不会到达另一份。

WHY 用 AST 而不是 ``sys.modules``：静态解析与 ``.importlinter`` 的口径一致
（两者都只认源码里写了什么），且覆盖 ``if TYPE_CHECKING:`` 块与函数内的
延迟导入——历史上 13 处 ``interfaces → runtime`` 越界正是靠延迟导入藏住的。
另外，注释里出现的模块名不会被误判（AST 不含注释节点）：本仓库的 docstring
大量讨论彼此的依赖关系，按文本匹配实现会立刻产生误报。

本模块不是测试文件（名字不以 ``test_`` 开头），pytest 不会收集它。
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
"""仓库根目录。本文件位于 ``tests/`` 下一级，故取上一级。"""

LAYER_PACKAGES: frozenset[str] = frozenset(
    {"agent", "application", "bootstrap", "interfaces", "llm", "runtime"}
)
"""六个分层包，与 ``.importlinter`` 的 ``root_packages`` 同源。

``tests/test_root_module_contract.py`` 直接引用本常量，避免两处各写一份后
在新增包时只改一处。
"""


@dataclass(frozen=True)
class ImportRecord:
    """源码里的一条导入语句。

    WHY 保留 ``lineno``：失败信息要能直接给出"哪一行"，否则作者得先把模块名
    转成文件、再逐行搜——这一步的成本正是"看见失败也不改"的起点。

    Attributes:
        module: 被导入的模块路径，如 ``"runtime.thread_store"``；
            相对导入时是相对的尾部（``from .x import y`` 为 ``"x"``）。
        name: 被导入的符号名；``import a.b`` 形式为空串。
        level: 相对导入层级（``from . import x`` 为 1）；绝对导入为 0。
        lineno: 语句所在行号。
    """

    module: str
    name: str
    level: int
    lineno: int

    @property
    def target(self) -> str:
        """返回可读的导入目标，供失败信息使用。"""
        prefix = "." * self.level
        if self.name:
            return f"{prefix}{self.module}:{self.name}"
        return f"{prefix}{self.module}" if self.module else f"{prefix}<relative>"


def parse_module(module_path: Path) -> ast.Module:
    """读取并解析一个模块文件。

    Args:
        module_path: 模块文件路径。

    Returns:
        解析得到的 AST。

    Raises:
        ValueError: 路径不是文件（调用方传错了对象，属编程错误）。
        AssertionError: 文件读取失败或源码无法解析。两者都会让调用方拿到空结果，
            进而让所有断言"通过"——所以必须在源头显式失败。
    """
    if not module_path.is_file():
        raise ValueError(f"模块文件不存在：{module_path}")

    try:
        source = module_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.exception("读取模块失败：%s", module_path)
        raise AssertionError(f"无法读取 {module_path}：{exc}") from exc

    try:
        return ast.parse(source, filename=str(module_path))
    except SyntaxError as exc:
        logger.exception("解析模块失败：%s", module_path)
        raise AssertionError(
            f"{module_path} 无法解析（第 {exc.lineno} 行）：{exc.msg}"
        ) from exc


def iter_imports(tree: ast.Module) -> Iterator[ImportRecord]:
    """遍历 AST，产出其中的全部导入语句。

    WHY 用 ``ast.walk`` 而不是只遍历 ``tree.body``：函数内的延迟导入与
    ``if TYPE_CHECKING:`` 块同样构成依赖，而它们恰好是历史上用来藏越界的位置。

    Args:
        tree: 已解析的模块 AST。

    Yields:
        按 AST 遍历顺序产出的导入记录；``from a import b, c`` 会产出两条。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield ImportRecord(module=alias.name, name="", level=0, lineno=node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if not node.names:
                continue
            module = node.module or ""
            for alias in node.names:
                yield ImportRecord(
                    module=module, name=alias.name, level=node.level, lineno=node.lineno
                )


def imported_symbols(module_path: Path) -> tuple[ImportRecord, ...]:
    """返回一个模块里的全部导入记录。

    Args:
        module_path: 模块文件路径。

    Returns:
        导入记录元组；没有任何导入时为空元组。

    Raises:
        ValueError: 路径不是文件。
        AssertionError: 文件读取失败或无法解析。
    """
    return tuple(iter_imports(parse_module(module_path)))


def package_modules(package: str) -> list[Path]:
    """返回某个分层包下的全部模块文件。

    Args:
        package: 分层包名（如 ``"application"``）。

    Returns:
        按路径排序的 ``*.py`` 文件列表。

    Raises:
        AssertionError: 一个都没扫到——说明包名写错或目录被移动，
            而依赖它的断言会因为"无可检查"而全部通过。
    """
    paths = sorted((ROOT / package).rglob("*.py"))
    if not paths:
        raise AssertionError(f"未在 {ROOT / package} 扫到任何模块文件，契约前提不成立")
    return paths


def top_level_name(module_path: Path) -> str:
    """返回模块所属的顶层名字（包名或根级模块名）。

    Args:
        module_path: 仓库内的模块文件。

    Returns:
        顶层名字。``runtime/thread_store.py`` 得到 ``runtime``；
        根目录的 ``web_tools.py`` 得到 ``web_tools``。

    Raises:
        ValueError: 路径不在仓库内——相对路径算不出来时无从判定归属，
            静默返回空串会让调用方把它当成"不认识的名字"放过去。
    """
    try:
        relative = module_path.resolve().relative_to(ROOT)
    except ValueError as exc:
        raise ValueError(f"{module_path} 不在仓库 {ROOT} 内") from exc
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def repo_top_level_imports(module_path: Path, known: frozenset[str]) -> frozenset[str]:
    """返回一个模块引用的仓库内顶层模块名。

    WHY 要传 ``known`` 而不是在本模块内置名单：调用方（根级模块角色表）的
    守备范围是它的角色表决定的，把名单写死在这里会让新增一个根级模块时
    需要改两个文件。

    Args:
        module_path: 待解析的模块文件。
        known: 视为"仓库内"的顶层名字集合。

    Returns:
        与 ``known`` 取交集后的顶层模块名，例如 ``{"agent", "config"}``。

    Raises:
        ValueError: 路径不是文件。
        AssertionError: 文件读取失败或源码无法解析。
    """
    names: set[str] = set()
    for record in imported_symbols(module_path):
        # 相对导入的 module 首段是同包内的子模块名（如 runtime 内的 ".thread_store"
        # 得到 "thread_store"），它不可能是顶层模块名，取交集时自然被过滤——
        # 这里显式跳过只是为了让意图清楚，不改变结果。
        if record.level:
            continue
        if record.module:
            names.add(record.module.split(".")[0])
    return frozenset(names & known)


__all__ = [
    "LAYER_PACKAGES",
    "ROOT",
    "ImportRecord",
    "imported_symbols",
    "iter_imports",
    "package_modules",
    "parse_module",
    "repo_top_level_imports",
    "top_level_name",
]
