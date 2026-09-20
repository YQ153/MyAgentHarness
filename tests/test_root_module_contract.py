"""根级模块的依赖边界契约。

WHY 这份契约不在 `.importlinter` 里：import-linter 只接受「包」作为分析根——
`root_packages` 必须是含 `__init__.py` 的目录。单文件模块既不能列为分析根
（报 `root_packages should only contain packages`），也不能被契约引用
（报 `Module 'config' does not exist`），两条均以 import-linter 2.15 / grimp 3.17
实测确认。而根级模块恰恰是最需要约束的一批：它们不属于任何包，因此不被任何
既有契约看见，可以任意 import 任何一层而不触发任何检查。

WHY 值得钉：这批模块在根级的**唯一理由**是「不被反向依赖」——
`text_utils` / `thread_utils` 被 `application` 与 `runtime` 同时使用，
放进任一层都会立刻构成循环（见两个模块各自的 docstring）。角色一旦被破坏，
症状不是这里报错，而是「某个 import 顺序下才崩」的循环导入，或契约 3
（`runtime` 是叶子）被从根级模块绕开。

WHY 用 AST 而不是「导入后检查 `sys.modules`」：静态解析与 `.importlinter` 口径一致
（两者都只认源码里写了什么），且覆盖 `if TYPE_CHECKING:` 块与函数内延迟导入——
历史上 `interfaces` 对 `runtime` 的 13 处越界正是靠延迟导入藏住的。

WHY 作用范围不含 `scripts/` 与 `tests/`：前者是一次性探针，后者按被测对象组织，
两者都不参与运行期装配，约束它们只会产生噪音。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]

#: 分层的六个包，与 `.importlinter` 的 `root_packages` 同源。
_LAYER_PACKAGES = frozenset({"agent", "application", "bootstrap", "interfaces", "llm", "runtime"})

#: 每个根级模块允许依赖的仓库内顶层模块。
#:
#: WHY 是「精确集合」而不是「上限」：多出一项是越界，少掉一项是表格过时。
#: 两个方向都要求动手更新，表格才不会退化成一份「历史记录」——那正是架构文档
#: 变成摆设的路径。失败信息里写明了两个方向该怎么做。
_BOUNDARIES: dict[str, frozenset[str]] = {
    # 中立叶子：存在的唯一理由就是「谁都可以依赖它，它不依赖任何一层」。
    # 一旦它依赖了某一层，下沉的理由（避免 application <-> runtime 循环）当场失效。
    "config": frozenset(),
    "text_utils": frozenset(),
    "thread_utils": frozenset(),
    "web_safety": frozenset(),
    # 服务句柄：整进程唯一的知识库实例。装配与拆除的所有权在 bootstrap，
    # 取用方是知识库插件（工具扩展点没有注入依赖的通道，只能自取）。
    "knowledge_runtime": frozenset({"application", "config", "llm", "runtime"}),
    # 工具插件：运行期被动态加载进 Agent 进程，因此只能依赖注册 SPI（agent.tools）、
    # 中立模块（config / text_utils / web_safety）与自己的服务句柄。
    # 直接摸 runtime / application 会绕过应用层的归属校验、审计与限流，
    # 而插件正是最暴露于提示词注入的那部分代码。
    #
    # text_utils 是后加的（2026-09-20）：两个插件原本各写一份截断判断，
    # 收敛到中立模块后多出这条边。它不破坏角色——text_utils 自身零仓库内依赖
    # （见上面的中立叶子条目），与已在允许集里的 config / web_safety 同类。
    "knowledge_tools": frozenset({"agent", "config", "knowledge_runtime", "text_utils"}),
    "web_tools": frozenset({"agent", "config", "text_utils", "web_safety"}),
    # 入口分发：解析参数后交给适配器，装配由 interfaces -> bootstrap 完成。
    # 若入口自己装配，契约 6 想避免的「第二套组装」会从 main.py 长回来。
    "main": frozenset({"application", "config", "interfaces"}),
}

_KNOWN_TOP_LEVEL = _LAYER_PACKAGES | frozenset(_BOUNDARIES)

#: 允许取用知识库服务句柄的顶层模块。
#:
#: WHY 要单独列：`_BOUNDARIES` 约束的是「根级模块能依赖谁」，这是另一个方向——
#: 「谁能依赖某个根级模块」。少了这一半，服务句柄会变成后门：
#: 任意一层（含运行期被动态加载的插件）都能直接拿到它，绕过应用层的归属校验与审计。
#: 只有两处是正当的：装配层建立它并负责拆除，知识库插件在没有注入通道的情况下取用它。
_HANDLE_CALLERS = frozenset({"bootstrap", "knowledge_tools"})

#: 服务句柄本身，被上面的规则保护。
_HANDLE_MODULE = "knowledge_runtime"


def _root_module_paths() -> list[Path]:
    """返回仓库根目录下的所有模块文件，按文件名排序。

    Returns:
        根目录 `*.py` 的路径列表。

    Raises:
        AssertionError: 一个都没找到。此时 `ROOT` 推导错了，而依赖它的用例会
            因为「没有模块可查」而全部通过——这类静默失效必须在源头拦住，
            否则整份契约会在解释器升级或目录调整后无声失效。
    """
    paths = sorted(ROOT.glob("*.py"))
    if not paths:
        raise AssertionError(f"未在 {ROOT} 找到任何模块文件，本契约的前提不成立")
    return paths


def _repo_imports(module_path: Path) -> frozenset[str]:
    """静态解析模块源码，返回它引用的仓库内顶层模块名。

    只统计仓库内的名字：标准库不属于本契约的范围（由解释器保证），
    三方依赖由 `uv.lock` 保证。

    Args:
        module_path: 待解析的模块文件。

    Returns:
        被引用的仓库内顶层模块名集合，例如 ``{"agent", "config"}``。

    Raises:
        ValueError: 路径不是文件。
        AssertionError: 文件读取失败或源码无法解析。两者都会让收集结果为空，
            从而让所有断言「通过」——所以都必须显式失败，不能吞掉。
    """
    if not module_path.is_file():
        raise ValueError(f"模块文件不存在：{module_path}")

    try:
        source = module_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.exception("读取根级模块失败：%s", module_path)
        raise AssertionError(f"无法读取 {module_path.name}：{exc}") from exc

    try:
        tree = ast.parse(source, filename=str(module_path))
    except SyntaxError as exc:
        logger.exception("解析根级模块失败：%s", module_path)
        raise AssertionError(
            f"{module_path.name} 无法解析（第 {exc.lineno} 行）：{exc.msg}"
        ) from exc

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # 相对导入（level > 0）在根级模块里是非法的：没有父包可回退。
            # 它的 node.module 可能为 None，会被下面的交集直接过滤掉，
            # 因而由 test_every_root_module_declares_a_role 之外的用例覆盖不到——
            # 这类写法会让模块在导入时直接抛 ImportError，不属本契约的职责。
            names.add(node.module.split(".")[0])
    return frozenset(names & _KNOWN_TOP_LEVEL)


def _top_level_name(module_path: Path) -> str:
    """返回模块所属的顶层名字（包名或根级模块名）。

    Args:
        module_path: 仓库内的模块文件。

    Returns:
        顶层名字。``runtime/thread_store.py`` 得到 ``runtime``；
        根目录的 ``web_tools.py`` 得到 ``web_tools``。

    Raises:
        ValueError: 路径不在仓库内——相对路径算不出来时无从判定归属，
            静默返回空串会让调用方把它当成「不认识的名字」放过去。
    """
    try:
        relative = module_path.resolve().relative_to(ROOT)
    except ValueError as exc:
        raise ValueError(f"{module_path} 不在仓库 {ROOT} 内") from exc
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def _scannable_paths() -> list[Path]:
    """返回参与运行期装配的模块文件。

    范围为六个分层包 + 八个根级模块。

    WHY 排除 `scripts/` 与 `tests/`：前者是一次性探针，后者是测试本身；
    它们不参与运行期装配，其中 `scripts/inspect_thread.py` 与
    `tests/test_knowledge_tools.py` 确实会导入服务句柄，且是正当的。

    Returns:
        按路径排序的模块文件列表。

    Raises:
        AssertionError: 一个都没扫到——说明 `ROOT` 推导错了，
            后面的断言会因「无可检查」而全部通过。
    """
    paths: list[Path] = []
    for package in sorted(_LAYER_PACKAGES):
        paths.extend(sorted((ROOT / package).rglob("*.py")))
    paths.extend(ROOT / f"{name}.py" for name in sorted(_BOUNDARIES))

    existing = [path for path in paths if path.is_file()]
    if not existing:
        raise AssertionError(f"未在 {ROOT} 下扫到任何模块文件，本契约的前提不成立")
    return existing


@pytest.mark.parametrize("module_name", sorted(_BOUNDARIES))
def test_root_module_imports_stay_within_its_role(module_name: str) -> None:
    """根级模块只允许依赖它声明的角色范围。

    两个方向各自断言：越界依赖会破坏该角色存在的理由；而角色表里留着已经
    不存在的依赖，会让下一次修改的人误以为那里还有一条边。
    """
    allowed = _BOUNDARIES[module_name]
    actual = _repo_imports(ROOT / f"{module_name}.py")

    forbidden = sorted(actual - allowed)
    assert not forbidden, (
        f"{module_name}.py 依赖了 {forbidden}，超出它声明的角色范围 {sorted(allowed)}。\n"
        "根级模块的角色（本表是它们唯一的约束）：\n"
        "  中立叶子 config / text_utils / thread_utils / web_safety：不依赖仓库内任何模块；\n"
        "  服务句柄 knowledge_runtime：只可依赖 application / llm / runtime / config；\n"
        "  工具插件 web_tools / knowledge_tools：只可依赖注册 SPI、中立模块与自己的服务句柄；\n"
        "  入口 main：只可依赖 config 与 interfaces / application。\n"
        "若新依赖确有正当理由，先确认它不破坏该角色的存在理由（见模块 docstring），"
        "再更新 _BOUNDARIES。"
    )

    stale = sorted(allowed - actual)
    assert not stale, (
        f"{module_name}.py 的 _BOUNDARIES 里声明了 {stale}，但代码里已经没有这条依赖。\n"
        "角色表是规格而不是历史记录：请把它删掉，否则下一个读表的人会以为这条边还在。"
    )


def test_every_root_module_declares_a_role() -> None:
    """根目录下不允许存在未声明角色的模块。

    这条守的是「盲区不再新增」：本契约的价值来自覆盖面，而覆盖面会随着
    仓库根新增一个文件而悄悄缩小——新文件不需要修改任何既有代码，
    因此不会有任何东西提醒作者它还缺一条约束。
    """
    found = {path.stem for path in _root_module_paths()}
    undeclared = sorted(found - set(_BOUNDARIES))

    assert not undeclared, (
        f"仓库根存在未声明角色的模块：{undeclared}。\n"
        "根级模块不受 .importlinter 约束（它只分析包），因此每个都必须在这里登记，"
        "否则它就能任意依赖任何一层而不被发现。"
        "请为它选定一个角色（中立叶子 / 服务句柄 / 工具插件 / 入口）并补进 _BOUNDARIES，"
        "同时在其 docstring 里写清「为什么必须在根级」。"
    )


def test_service_handle_is_reached_only_by_its_legitimate_callers() -> None:
    """服务句柄只能由装配层与它的插件取用。

    上面几条约束的是「根级模块能依赖谁」，这条约束的是反方向：「谁能依赖根级模块」。
    缺了这一半，服务句柄就成了后门——任何一层、任何被动态加载的插件都可以直接取用
    一个活的 SQLite 连接，绕过应用层的归属校验、审计与限流，而这些都是建立在
    「服务只经由应用层被访问」这个前提上的。
    """
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}（属于 {_top_level_name(path)}）"
        for path in _scannable_paths()
        if _top_level_name(path) not in _HANDLE_CALLERS
        and _HANDLE_MODULE in _repo_imports(path)
    ]

    assert not offenders, (
        f"以下模块直接依赖了 {_HANDLE_MODULE}：{offenders}\n"
        f"合法取用方只有 {sorted(_HANDLE_CALLERS)}："
        "装配层建立它并负责拆除，知识库插件因扩展点没有注入依赖的通道而取用它。\n"
        "其余模块应当经由应用层获得服务——请改走 AppContext 注入，"
        "若确实需要新增一个取用方，先确认它不会绕过应用层。"
    )
