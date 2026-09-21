"""application 层的层内依赖契约：分组、纯度与对外声明。

``.importlinter`` 的 6 条契约全部作用在包与包之间（``root_packages`` 是 6 个包），
包**内部**平铺模块之间"谁可以依赖谁"没有任何约束——本条契约补的就是这个盲区
（与 ``tests/test_root_module_contract.py`` 补根级模块盲区是同一形态）。

三条断言分别守三件事：

1. **层内纯度**：对外契约组（``dto`` / ``errors`` / ``events`` / ``ports`` /
   ``audit_recorder``）不得依赖服务组模块。
   契约组的变更理由是"对外契约变化"，服务组的是"业务流程变化"；后者依赖前者是
   正常的，**前者依赖后者**说明契约层被业务流程污染——那正是服务之间互相 import、
   最终形成包内环的开端。
2. **分组覆盖完整**：新增一个模块必须在这里登记，否则它既不受纯度约束，
   也不会被 ``application/__init__.py`` 的 docstring 描述。
3. **对外声明与事实一致**：``application.__doc__`` 必须逐一点名全部模块，
   ``__all__`` 里的每个符号都必须真的能导入。

WHY 三条放在一个文件：它们共用同一份分组表。拆成三个文件会让"新增一个模块"
需要在多处各改一遍，而漏改的那一处正是约束失效的地方。

WHY 用 AST 静态解析（``tests/_ast_imports.py``）而不是导入后检查 ``sys.modules``：
延迟导入（函数内）与 ``if TYPE_CHECKING:`` 块同样构成依赖，而它们恰好是历史上
用来藏越界的位置；此外，注释里提到模块名不会被误判（AST 不含注释节点）——
本层的 docstring 大量讨论彼此的依赖关系，按文本匹配会立刻产生误报。
"""

from __future__ import annotations

import importlib
import logging
import re

from tests._ast_imports import (
    ROOT,
    ImportRecord,
    imported_symbols,
    package_modules,
)

logger = logging.getLogger(__name__)

#: 对外契约组：无状态、被服务组依赖，本身不依赖任何服务。
_CONTRACT_MODULES: frozenset[str] = frozenset(
    {
        "dto",
        "errors",
        "events",
        "ports",
        "audit_recorder",
    }
)

#: 服务组：承载业务流程，可依赖契约组。
_SERVICE_MODULES: frozenset[str] = frozenset(
    {
        # 会话与运行编排
        "thread_service",
        "run_service",
        "run_branch",
        "run_governance",
        "run_registry",
        # 领域能力服务
        "attachment_service",
        "knowledge_service",
        "memory_service",
        "skill_service",
        "usage_service",
        "workspace_service",
        "session_registry",
        "model_catalog",
        "tool_catalog",
        "health",
        # 协议翻译与支撑
        "event_translator",
        "interrupt_codec",
        "usage",
        "runnable",
        "thread_export",
        "message_utils",
        "audit_context",
    }
)

#: 分组表与 ``application/__init__.py`` 的 docstring 是同一条规格的两处载体。
_APPLICATION_MODULES: frozenset[str] = _CONTRACT_MODULES | _SERVICE_MODULES

#: 从 docstring 中提取双反引号包裹的名字，用于校验"声明与事实一致"。
_DOCSTRING_TOKEN_RE = re.compile(r"``([a-z_]+)``")


def _actual_module_names() -> frozenset[str]:
    """返回 ``application`` 包内实际存在的模块名。

    Returns:
        模块名集合（不含 ``__init__``）；子包以其包名计入。

    Raises:
        AssertionError: 一个模块都没扫到——此时后面的断言会因"无可检查"而全部通过，
            契约整体空转，必须显式失败。
    """
    names: set[str] = set()
    for path in package_modules("application"):
        relative = path.relative_to(ROOT / "application")
        if relative.name == "__init__.py":
            continue
        # 子包（若将来引入）以其包名计入分组表，与其内部文件数无关。
        names.add(relative.parts[0] if len(relative.parts) > 1 else relative.stem)

    if not names:
        raise AssertionError("未扫到 application 下的任何模块，本契约的前提不成立")
    return frozenset(names)


def _application_targets(record: ImportRecord) -> frozenset[str]:
    """返回一条导入语句指向的 ``application`` 包内模块名。

    WHY 要同时处理绝对与相对导入：两种写法在本层内都可能出现，只覆盖其中一种
    会让越界从另一种形式溜过去（这正是"契约看起来被覆盖了"的典型成因）。

    Args:
        record: 一条导入记录。

    Returns:
        包内目标模块名集合；与 ``application`` 无关时为空集。
    """
    targets: set[str] = set()

    if record.level:
        # 相对导入：`from .dto import X` 的 module 是 "dto"；`from . import dto`
        # 的 module 为空、模块名落在 name 上。
        head = record.module.split(".")[0] if record.module else record.name
        if head:
            targets.add(head)
        return frozenset(targets)

    if record.module == "application":
        targets.add(record.name)
    elif record.module.startswith("application."):
        targets.add(record.module.split(".")[1])
    return frozenset(targets)


def test_contract_modules_do_not_depend_on_service_modules() -> None:
    """契约组模块不得依赖服务组模块。

    这是本文件的核心断言。实测（2026-09-20）当前为 0 违规——它在库里没有
    可举证的坏味道，而是把"契约层保持纯净"从共识变成 CI 会拦的规则。
    """
    offenders: list[str] = []
    checked = 0

    for module_name in sorted(_CONTRACT_MODULES):
        module_path = ROOT / "application" / f"{module_name}.py"
        for record in imported_symbols(module_path):
            checked += 1
            hits = sorted(_application_targets(record) & _SERVICE_MODULES)
            if hits:
                offenders.append(
                    f"{module_path.relative_to(ROOT).as_posix()}:{record.lineno} "
                    f"导入了 {hits}（{record.target}）"
                )

    logger.debug("层内纯度检查完成：契约组模块 %d 个，导入语句 %d 条", len(_CONTRACT_MODULES), checked)

    assert not offenders, (
        "以下契约组模块依赖了服务组模块：\n  "
        + "\n  ".join(offenders)
        + "\n契约组（dto / errors / events / ports / audit_recorder）"
        "描述的是对外契约，它对服务一无所知；反过来依赖服务，说明契约里混进了业务流程。\n"
        "正确方向是「服务依赖契约」。若确实需要共享某段判定，"
        "把它下沉到契约组模块或根级中立模块（见 README 的分层约定）。"
    )


def test_role_table_covers_every_application_module() -> None:
    """分组表必须与包内实际模块集合逐项相等。

    两个方向都要断言：漏登记的新模块不受任何约束（约束覆盖面悄悄缩小）；
    表里留着已删除的模块，会让下一个读表的人以为它还在——这与
    ``tests/test_root_module_contract.py`` 的角色表纪律一致。
    """
    actual = _actual_module_names()

    undeclared = sorted(actual - _APPLICATION_MODULES)
    assert not undeclared, (
        f"application 下存在未登记的模块：{undeclared}。\n"
        "本契约按这份分组表判定内聚关系，未登记的新模块既不受纯度约束，"
        "也不会出现在 application/__init__.py 的 docstring 里。\n"
        "请把它归入契约组或服务组，并同步更新 docstring（两处必须一起改）。"
    )

    stale = sorted(_APPLICATION_MODULES - actual)
    assert not stale, (
        f"分组表里声明了 {stale}，但 application 下已没有这些模块。\n"
        "分组表是规格而不是历史记录：请删掉它们，否则下一个读表的人会以为这些边还在。"
    )


def test_module_docstring_names_every_module() -> None:
    """``application/__init__.py`` 的 docstring 必须点名每一个模块。

    WHY 值得钉：这一层有二十余个模块，而 ``__all__`` 只导出 12 个符号。旧版 docstring
    写着"对外提供三件事"，新读者按它理解会漏掉二十余个模块。声明与事实不一致时，
    失败的表现是"找不到东西"，而不是任何一处报错。
    """
    application = importlib.import_module("application")
    docstring = application.__doc__ or ""
    assert docstring.strip(), "application/__init__.py 缺少 docstring——它是本层唯一的入口说明"

    tokens = {match.group(1) for match in _DOCSTRING_TOKEN_RE.finditer(docstring)}
    covered = tokens & _actual_module_names()
    missing = sorted(_actual_module_names() - covered)

    assert not missing, (
        f"application/__init__.py 的 docstring 未点名以下模块：{missing}。\n"
        "docstring 里的职责域清单是这一层唯一的入口说明，漏掉模块会让读者以为它不存在；\n"
        "若模块已被删除，请一并从 docstring 与分组表中移除。"
    )


def test_exported_names_are_importable() -> None:
    """``__all__`` 里的每个符号都必须真的能导入。

    WHY：``__all__`` 是给接口层看的契约。写了一个不存在的名字时，失败发生在
    使用方 ``from application import X`` 的那一刻，而这里能让它提前失败。
    """
    application = importlib.import_module("application")
    exported = list(getattr(application, "__all__", []))
    assert exported, "application/__init__.py 的 __all__ 为空——对外契约面不该是空的"

    missing = sorted(name for name in exported if not hasattr(application, name))
    assert not missing, (
        f"application.__all__ 里的这些符号无法导入：{missing}。\n"
        "要么补齐导入，要么从 __all__ 中移除（它声称是稳定契约，不能指向不存在的东西）。"
    )


__all__ = [
    "test_contract_modules_do_not_depend_on_service_modules",
    "test_exported_names_are_importable",
    "test_module_docstring_names_every_module",
    "test_role_table_covers_every_application_module",
]
