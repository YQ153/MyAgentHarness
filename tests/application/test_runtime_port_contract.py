"""应用层的运行时端口契约：哪些 ``runtime`` 依赖是有意保留的。

背景（见《架构遗留问题治理方案》Q8 / 方案八）：``application/ports.py`` 的协议
让应用层不再依赖 ``runtime`` 的**可替换实现类**——换一种存储实现时，改动只落在
``bootstrap``。但并非所有 ``runtime`` 依赖都该端口化：

- **无状态函数与上下文管理器**（``workspace_files`` / ``tool_outputs`` /
  ``execution_registry`` / ``skill_view`` / ``skills`` / ``attachments`` /
  ``window_start`` / ``normalize_search_query``）没有"另一种实现"的诉求；
  其中 ``workspace_files`` 是**路径校验这一安全边界的唯一实现**，
  为它引入协议只会诱发第二份实现（各服务 docstring 反复强调这一点）。
- **数据载体与异常**（``ChunkInput`` / ``KnowledgeHit`` / ``EmbeddingError``）
  的形状由两侧共同理解，与存储技术无关；捕获特定异常类型也必须拿到类。
- **``RateLimiter``** 由 ``RunRegistry`` 自建：``run_registry.py`` 已论证
  "限流器按运行期才算出的主体做键，配置只读一处"，改为注入会与该理由冲突。

WHY 需要一个契约而不是"在 docstring 里写清楚"：白名单之外的人只需要加一行
import 就能回到端口化之前的状态，而那时没有任何东西会提醒他。
本文件把"哪些是允许的"变成精确集合，两个方向都断言：
越界会失败，**白名单里的过时条目也会失败**（否则表格会退化成历史记录）。

WHY 用 AST 静态解析（``tests/_ast_imports.py``）：延迟导入与
``if TYPE_CHECKING:`` 块同样构成依赖，而它们正是历史上用来藏越界的位置。
"""

from __future__ import annotations

import logging

from tests._ast_imports import ROOT, imported_symbols, package_modules

logger = logging.getLogger(__name__)

#: 允许应用层直接依赖的 ``runtime`` 模块及其符号（精确集合，不是上限）。
#:
#: 每一条都对应上面 docstring 里的一个类别；新增条目必须先回答"它为什么
#: 不是可替换的实现"，而不是直接加一行。以 ``Store`` 结尾的实现类由
#: ``test_whitelist_does_not_smuggle_store_classes`` 单独拦一道。
_ALLOWED_RUNTIME_IMPORTS: dict[str, frozenset[str]] = {
    # 附件：函数式存储（Protocol 无法约束模块对象，见 ports.py 的说明）
    "runtime.attachments": frozenset(
        {
            "AttachmentError",
            "AttachmentRecord",
            "count_attachments",
            "delete_attachment",
            "delete_thread_attachments",
            "index_by_sha256",
            "list_attachments",
            "load_attachment",
            "save_attachment",
        }
    ),
    # 执行作用域：协作者之间传递的上下文管理器
    "runtime.execution_registry": frozenset({"abort_scope", "bound_scope"}),
    # 知识库的数据载体（KnowledgeStore 本身已端口化为 KnowledgeIndex）
    "runtime.knowledge_store": frozenset({"ChunkInput", "KnowledgeHit"}),
    # 运行限流器：由 RunRegistry 自建，理由见 run_registry.py 的构造注释
    "runtime.rate_limiter": frozenset({"RateLimiter"}),
    # 技能状态常量（SkillStateStore 本身已端口化为 SkillState）
    "runtime.skill_store": frozenset({"DEFAULT_ENABLED", "GLOBAL_SCOPE"}),
    # 技能视图：只读的派生产物读写
    "runtime.skill_view": frozenset(
        {"ViewEntry", "ViewResult", "rebuild_view", "sources_for_graph", "view_directory"}
    ),
    # 技能包解析
    "runtime.skills": frozenset({"inspect_skills"}),
    # 会话 ID 规范化的权威实现（ThreadMetaStore 本身已端口化）
    "runtime.thread_store": frozenset({"normalize_search_query"}),
    # 工具输出的落盘与回收
    "runtime.tool_outputs": frozenset(
        {"prune_tool_outputs", "tool_output_path", "write_tool_output"}
    ),
    # 用量时间窗换算（UsageStore 本身已端口化为 UsageLedger）
    "runtime.usage_store": frozenset({"window_start"}),
    # 工作区路径解析：安全边界的唯一实现，必须只有一处
    "runtime.workspace_files": frozenset(
        {
            "IMAGE_SUFFIXES",
            "WorkspacePathError",
            "list_directory",
            "looks_binary",
            "read_bytes_capped",
            "resolve_in_workspace",
            "to_virtual_path",
        }
    ),
}


def _runtime_imports() -> dict[str, set[str]]:
    """收集应用层全部对 ``runtime`` 的导入。

    Returns:
        ``{runtime 模块路径: {符号名}}``；``import runtime`` 形式的符号名为空串。

    Raises:
        AssertionError: 应用层一个模块都没扫到——此时所有断言都会因"无可检查"
            而通过，契约整体空转，必须显式失败（与 ``tests/_ast_imports.py``
            的纪律一致）。
    """
    collected: dict[str, set[str]] = {}
    for path in package_modules("application"):
        for record in imported_symbols(path):
            # 相对导入指向同包内的模块，不可能是 runtime。
            if record.level or not record.module.startswith("runtime"):
                continue
            collected.setdefault(record.module, set()).add(record.name)
    return collected


def _offending_imports() -> list[str]:
    """返回越界的 ``runtime`` 导入（模块不在白名单，或符号未被允许）。"""
    offenders: list[str] = []
    for path in package_modules("application"):
        for record in imported_symbols(path):
            if record.level or not record.module.startswith("runtime"):
                continue
            allowed = _ALLOWED_RUNTIME_IMPORTS.get(record.module)
            if allowed is None or record.name not in allowed:
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}:{record.lineno} "
                    f"导入了 {record.target}"
                )
    return offenders


def test_application_imports_only_whitelisted_runtime_symbols() -> None:
    """应用层只能依赖白名单内的 ``runtime`` 模块与符号。

    越界意味着应用层重新依赖了某个可替换的实现——那就回到了端口化之前：
    换存储实现要改应用层的 import 与注解。
    """
    offenders = _offending_imports()
    logger.debug("运行时端口契约检查完成：越界导入 %d 处", len(offenders))

    assert not offenders, (
        "以下导入超出了应用层允许的 runtime 依赖白名单：\n  "
        + "\n  ".join(offenders)
        + "\n白名单的判据见本文件 docstring：可替换的实现类必须走 application.ports 的协议；"
        "无状态函数、数据载体与异常可以保留直接依赖。\n"
        "若这次新增的确实属于「可以保留」那一类，请把它加进 _ALLOWED_RUNTIME_IMPORTS，"
        "并在注释里写明它为什么不是可替换的实现。"
    )


def test_whitelist_has_no_stale_entries() -> None:
    """白名单里不允许存在已不再被使用的条目。

    WHY：留着不用的条目会让下一个读表的人以为那条边还在——"表格作为规格"
    的前提是它随时都等于事实。这与 ``tests/test_root_module_contract.py``
    的角色表纪律一致。
    """
    actual = _runtime_imports()

    stale_modules = sorted(set(_ALLOWED_RUNTIME_IMPORTS) - set(actual))
    stale_symbols = [
        f"{module}.{name}"
        for module, names in _ALLOWED_RUNTIME_IMPORTS.items()
        for name in sorted(names - actual.get(module, set()))
    ]

    assert not stale_modules, (
        f"白名单里的这些 runtime 模块已经没有任何应用层模块导入：{stale_modules}。\n"
        "请删掉它们——白名单是规格而不是历史记录。"
    )
    assert not stale_symbols, (
        f"白名单里的这些符号已经不再被导入：{stale_symbols}。\n"
        "请删掉它们，否则读者会以为应用层还依赖着它们。"
    )


def test_whitelist_does_not_smuggle_store_classes() -> None:
    """白名单里不得出现以 ``Store`` 结尾的实现类。

    WHY 单独一条：遇到越界时最省事的"修复"是把符号加进白名单。对可替换的
    存储实现来说那是错误的修法——正确的做法是在 ``application/ports.py``
    里加协议（``runtime`` 侧零改动，因为结构化子类型自动满足）。
    这条断言让「顺手放宽」必须正面回答这个问题。
    """
    smuggled = sorted(
        f"{module}.{name}"
        for module, names in _ALLOWED_RUNTIME_IMPORTS.items()
        for name in names
        if name.endswith("Store")
    )

    assert not smuggled, (
        f"白名单里出现了存储实现类：{smuggled}。\n"
        "可替换的实现类应当端口化：在 application/ports.py 增加协议，"
        "让服务标注协议而不是具体类；runtime 侧无需改动。"
    )


__all__ = [
    "test_application_imports_only_whitelisted_runtime_symbols",
    "test_whitelist_does_not_smuggle_store_classes",
    "test_whitelist_has_no_stale_entries",
]
