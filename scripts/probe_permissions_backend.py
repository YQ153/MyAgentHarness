"""技术验证：deepagents 的「工具级文件权限 × 可执行 backend」组合约束。

要回答的问题只有一个：**为什么 ``EXECUTION_MODE=local / sandbox`` 装不出一张图，
以及在不放弃执行能力的前提下有哪些合规解法。**

结论来自实测而非推断——本脚本可直接复跑复核：

1. 可执行 backend（``LocalShellBackend`` 及其子类）+ 任意权限规则 →
   ``NotImplementedError``。这是上游刻意为之：``execute`` 走 shell，
   可以绕过 ``read_file`` / ``write_file`` 的权限检查，于是上游拒绝
   「假装权限仍然生效」。
2. 同一 backend 不传权限规则 → 正常装配。
3. 例外通道：当**所有**规则路径都落在 ``CompositeBackend`` 的 route 前缀内时，
   上游认为权限作用域与可执行默认后端不重叠，因此放行（见
   ``_all_paths_scoped_to_routes``）。本项目的规则以 ``/**`` 作用于整个虚拟根，
   不满足该条件。
4. 非可执行 backend（``FilesystemBackend``）+ 现有权限规则 → 正常装配，
   权限照常生效。

因此解锁执行档位只能走「按 backend 能力裁剪权限规则」这条路；本脚本用于把
该判断固化为可复核的证据。

退出码：``0`` 全部符合预期；``1`` 有任一预期未被满足（上游行为已变，需重新评估）。
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
# WHY 强制 UTF-8 输出：Windows 控制台默认 GBK，异常文本里含非 GBK 字符时
# print 会抛 UnicodeEncodeError，把探针结论变成一条假失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from deepagents.backends import (  # noqa: E402
    CompositeBackend,
    FilesystemBackend,
    LocalShellBackend,
)
from deepagents.middleware.filesystem import (  # noqa: E402
    FilesystemMiddleware,
    FilesystemPermission,
    _all_paths_scoped_to_routes,
    supports_execution,
)

from agent.guardrails import build_permissions  # noqa: E402

logger = logging.getLogger("probe.permissions_backend")


def _composite(default, root: pathlib.Path) -> CompositeBackend:
    """构造与生产同形的 CompositeBackend：默认后端可为可执行 / 纯文件系统。

    WHY 一定要用 CompositeBackend 而不是裸 backend：生产装配就是
    ``CompositeBackend(default=..., routes={"/memories/": ...})``，而豁免条件
    ``_all_paths_scoped_to_routes`` 只对 CompositeBackend 有意义。
    """
    return CompositeBackend(
        default=default,
        routes={"/memories/": FilesystemBackend(root_dir=str(root), virtual_mode=True)},
    )


def _try_build(backend: CompositeBackend, rules: list[FilesystemPermission]) -> str:
    """尝试装配中间件，返回 ``ok`` 或带异常类型的描述。"""
    try:
        FilesystemMiddleware(backend=backend, _permissions=rules)
    except NotImplementedError as exc:
        return f"NotImplementedError: {exc}"
    except Exception as exc:  # noqa: BLE001 - 探针要把非预期异常一并暴露出来
        return f"UNEXPECTED {type(exc).__name__}: {exc}"
    return "ok"


def main() -> int:
    """逐条验证四项预期，返回进程退出码。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

    rules = build_permissions()
    route_scoped = [
        FilesystemPermission(operations=["read", "write"], paths=["/memories/**"], mode="allow")
    ]

    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        executable = _composite(
            LocalShellBackend(root_dir=str(root), virtual_mode=True), root
        )
        plain = _composite(FilesystemBackend(root_dir=str(root), virtual_mode=True), root)

        print("=== 前提：backend 能力判定 ===")
        exec_supported = supports_execution(executable)
        plain_supported = supports_execution(plain)
        print(f"可执行 backend  supports_execution = {exec_supported}（预期 True）")
        print(f"纯文件 backend  supports_execution = {plain_supported}（预期 False）")
        if exec_supported is not True or plain_supported is not False:
            failures.append("supports_execution 判定与预期不符")

        print("\n=== 组合 1：可执行 backend + 生产权限规则 ===")
        scoped = _all_paths_scoped_to_routes(rules, executable)
        print(f"_all_paths_scoped_to_routes = {scoped}（预期 False，规则含 /** 全局路径）")
        result = _try_build(executable, rules)
        print(f"装配结果：{result}")
        if scoped is not False:
            failures.append("豁免判定与预期不符：本项目规则不应被放行")
        if not result.startswith("NotImplementedError"):
            failures.append("组合 1 未被拒绝，上游行为可能已变")

        print("\n=== 组合 2：可执行 backend + 不传权限规则 ===")
        result = _try_build(executable, [])
        print(f"装配结果：{result}（预期 ok）")
        if result != "ok":
            failures.append("组合 2 未能装配，解锁路径不成立")

        print("\n=== 组合 3：可执行 backend + 路由内权限规则（豁免通道）===")
        scoped = _all_paths_scoped_to_routes(route_scoped, executable)
        print(f"_all_paths_scoped_to_routes = {scoped}（预期 True）")
        result = _try_build(executable, route_scoped)
        print(f"装配结果：{result}（预期 ok）")
        if scoped is not True or result != "ok":
            failures.append("豁免通道与预期不符")

        print("\n=== 组合 4：纯文件 backend + 生产权限规则 ===")
        result = _try_build(plain, rules)
        print(f"装配结果：{result}（预期 ok，权限照常生效）")
        if result != "ok":
            failures.append("组合 4 未能装配，disabled 档位的权限保护受影响")

    if failures:
        print("\n=== 结论：有预期未被满足 ===")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("\n=== 结论：四项预期全部成立 ===")
    print("解锁执行档位必须按 backend 能力裁剪权限规则：")
    print("可执行 backend 下不传工具级权限，凭据防护交由 execute 的人工审批与隔离承担。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
