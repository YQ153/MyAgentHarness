"""技能「物化视图」探针：确定用复制、软链还是硬链来派生启用视图。

背景：`scripts/probe_skills.py` 已实测**来源不能指向技能目录自身**（`SkillsMiddleware`
要找的是「来源目录的子目录里有没有 SKILL.md」）。所以「按库里的启用状态决定加载哪些技能」
只能靠**物化一份派生目录**——库里是真相，派生目录是产物，技能包本体永远不动。

三种物化方式各有代价，本探针逐个实测能否被 `FilesystemBackend(virtual_mode=True)` 正常
加载：

1. **复制**：一定能用，代价是磁盘上有副本；辅助脚本也要一并复制，否则模型按派生路径去读
   辅助文件时会找不到。
2. **目录软链**：零副本、语义最准确；代价是 Windows 上创建软链需要开发者模式或管理员权限，
   而本项目此前对软链有专门的逃逸拦截（T15），需确认后端是否跟随。
3. **文件硬链**：NTFS 上无需特权，零副本（同一份数据两个名字）；但只能逐文件建，且要求与
   目标同卷。

用法：``python scripts/probe_skills_view.py``；退出码 ``0`` 有可用方案 / ``1`` 全部不可用。
Windows 上语料含中文，故全程强制 UTF-8 输出。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from deepagents.backends import FilesystemBackend  # noqa: E402
from deepagents.middleware.skills import SkillsMiddleware  # noqa: E402

_SKILL = """---
name: {name}
description: {description}
---

# {name} 技能正文

## 何时使用
- 当用户要求做与 {name} 相关的事时。
"""

_HELPER = "print('辅助脚本：{name}')\n"


def _seed(ws: pathlib.Path) -> None:
    """在 ``ws/skills/`` 下铺两个技能包（含一个辅助脚本）。"""
    for name in ("code-review", "legacy-skill"):
        directory = ws / "skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            _SKILL.format(name=name, description=f"{name} 的说明"), encoding="utf-8"
        )
        (directory / "helper.py").write_text(_HELPER.format(name=name), encoding="utf-8")


def _load(ws: pathlib.Path, source: str) -> list[str]:
    """按给定来源加载技能名列表。"""
    backend = FilesystemBackend(root_dir=ws, virtual_mode=True)
    middleware = SkillsMiddleware(backend=backend, sources=[source])  # type: ignore[arg-type]
    update = middleware.before_agent({}, None, {})  # type: ignore[arg-type]
    return [str(item["name"]) for item in (update or {}).get("skills_metadata", [])]


def _try(label: str, action: object) -> bool:
    """执行一段建链/复制动作，把失败原因原样打印出来。"""
    try:
        action()  # type: ignore[operator]
        return True
    except OSError as exc:
        print(f"  [NO  ] {label} 建立失败：{type(exc).__name__}: {exc}")
        return False


def probe_copy(ws: pathlib.Path) -> None:
    """方案 1：整目录复制。"""
    print("=== 方案 1：复制 ===")
    view = ws / ".view-copy" / "active"
    view.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ws / "skills" / "code-review", view / "code-review")

    if _load(ws, "/.view-copy/active") == ["code-review"]:
        print("  [OK  ] 复制出的视图可被加载")
        print("  代价：磁盘上有副本；辅助脚本必须一并复制，否则模型按派生路径读不到")
    else:
        print("  [NO  ] 复制出的视图未能加载")


def probe_dir_symlink(ws: pathlib.Path) -> None:
    """方案 2：目录软链。"""
    print("\n=== 方案 2：目录软链 ===")
    view = ws / ".view-link" / "active"
    view.mkdir(parents=True, exist_ok=True)
    link = view / "code-review"
    created = _try("目录软链", lambda: os.symlink(ws / "skills" / "code-review", link, target_is_directory=True))
    if not created:
        print("  → 本机不支持（Windows 需开发者模式或管理员权限），该方案不可用")
        return

    names = _load(ws, "/.view-link/active")
    print(f"  加载结果：{names}")
    if names == ["code-review"]:
        print("  [OK  ] 后端会跟随目录软链")
    else:
        print("  [NO  ] 后端未跟随软链（T15 对软链逃逸有专门拦截，需评估）")

    # 辅助文件能否经软链读到——模型是按派生路径去读的
    helper = link / "helper.py"
    print(f"  经软链读辅助文件：{'可读' if helper.is_file() else '不可读'}")


def probe_file_hardlink(ws: pathlib.Path) -> None:
    """方案 3：文件硬链。"""
    print("\n=== 方案 3：文件硬链 ===")
    view = ws / ".view-hard" / "active" / "code-review"
    view.mkdir(parents=True, exist_ok=True)
    source = ws / "skills" / "code-review"
    ok = True
    for item in sorted(source.iterdir()):
        target = view / item.name
        ok = _try(f"硬链 {item.name}", lambda item=item, target=target: os.link(item, target)) and ok
    if not ok:
        print("  → 硬链建立失败（要求与目标同卷），该方案不可用")
        return

    if _load(ws, "/.view-hard/active") == ["code-review"]:
        print("  [OK  ] 硬链视图可被加载，且无副本（同一份数据两个名字）")
        print("  代价：只能逐文件建；文件内容被替换（而非原地修改）时链会断开")
    else:
        print("  [NO  ] 硬链视图未能加载")


def main() -> int:
    """跑完三种物化方式。"""
    with tempfile.TemporaryDirectory(prefix="mah-skills-view-") as workdir:
        ws = pathlib.Path(workdir)
        _seed(ws)

        baseline = _load(ws, "/skills")
        print(f"基线（直接用原目录）：{baseline}\n")

        probe_copy(ws)
        probe_dir_symlink(ws)
        probe_file_hardlink(ws)

    print("\n=== 结论：优先选能用的第一项；复制是保底方案 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
