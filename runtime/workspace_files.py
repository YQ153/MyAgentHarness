"""工作区文件访问：路径校验、列目录、按上限读文件。

WHY 单独成模块：「路径必须落在工作区内」这条判定必须**只有一份实现**。接口层
（文件面板）、工具层与将来的导出功能都要用它，各写一份的结果是某一处忘了校验——
而另外几处从代码上看不出缺口。

WHY 放在 ``runtime`` 而不是接口层：文件系统访问是基础设施；且分层约定要求
``interfaces → application → runtime``，把校验放进接口层会让服务层与测试都无法
复用它。

WHY 不能直接复用既有的两处实现：
- ``agent/path_safety.py`` 是 ``FilesystemBackend`` 的 Mixin，其 ``_resolve_path``
  依赖 backend 实例（``cwd`` / ``virtual_mode``），不是可独立调用的校验函数；
- ``application/memory_service.to_store_key`` 只做**字符串级**校验（拒绝 ``..`` 段、
  空字节），够用于 Store 键，但它不解析真实路径，挡不住**经符号链接 / 目录联接
  逃逸**——而工作区里恰恰可能有软链。
因此这里做的是解析级校验：先归一成相对路径，再 ``resolve()`` 后复检是否仍在根内。

路径形态：对外一律用**以 ``/`` 开头的虚拟路径**（``/react-vite-app/src/main.jsx``），
与 Agent 的虚拟文件系统视图一致，前端直接把列表里的 path 拼进 URL 即可。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from config import HARNESS_DIR_NAME

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_HIDDEN_ROOT_ENTRIES = frozenset({HARNESS_DIR_NAME})
"""列**工作区根**时不返回的条目名（应用自己的数据目录）。

WHY 需要隐藏（2026-09-22 新增）：应用数据（技能库 / 技能视图 / 工具留存 / 知识库索引）
回到了工作区内的 ``.harness/``，而文件面板列的就是真实目录——不隐藏的话，用户每次打开
面板都会先看到一屏「自己没建过的目录」；而技能视图每次重建都会刷新它的时间戳，看起来
像项目里有东西在不停变动。

WHY 只过滤**根层**、且按名字过滤：``.harness`` 是应用在根下固定的落点（名字来自
``config``，与 ``SessionRoot.storage_dir`` 同源）；在深层按名字过滤会误伤用户自己的同名
目录。
"""

TEXT_SUFFIXES = frozenset(
    {
        ".c",
        ".cfg",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".csv",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".log",
        ".md",
        ".mjs",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".svg",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
"""按扩展名判定的文本类型。

WHY 不用扩展名白名单之外的都当二进制：真正的判据是内容里有没有空字节（见
``looks_binary``），扩展名只用来决定「给不给代码高亮」。用扩展名当唯一判据会
让没有后缀的文本文件（``Makefile``、``LICENSE``）被误判成二进制而不可预览。
"""

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".avif"})
"""可直接内联预览的图片类型；与前端 ``<img>`` 能渲染的格式保持一致。"""


class WorkspacePathError(ValueError):
    """路径不合法或逃出了工作区。

    WHY 继承 ``ValueError``：调用方（服务层与路由）需要把它与「文件不存在」
    区分开——前者是非法输入（400），后者是资源缺失（404），可采取的动作不同。
    """


@dataclass(frozen=True)
class WorkspaceEntry:
    """目录项的一条元信息。"""

    name: str
    path: str
    """虚拟路径，可直接回传给接口。"""
    is_dir: bool
    size: int
    modified_at: float
    is_symlink: bool


@dataclass(frozen=True)
class WorkspaceListing:
    """一次目录列举的结果。"""

    path: str
    parent: str | None
    entries: tuple[WorkspaceEntry, ...]
    truncated: bool
    """是否因为条目数上限而截断。"""


def _normalize_virtual_path(virtual_path: str) -> str:
    """把输入归一成 ``/`` 开头的虚拟路径并做字符串级拦截。

    Raises:
        WorkspacePathError: 输入非法（空、非字符串、空字节、含 ``..`` 段、
            含 ``~``、含盘符或数据流冒号、解析后仍是绝对路径）。
    """
    if not isinstance(virtual_path, str):
        raise WorkspacePathError(f"path 必须是字符串，实际：{type(virtual_path).__name__}")

    # WHY 容忍反斜杠：Windows 上从资源管理器复制路径是常态，统一归一后
    # 报错信息才不会变成「路径不存在」这种误导性结论。
    raw = virtual_path.strip().replace("\\", "/")
    if not raw:
        return "/"
    if "\x00" in raw:
        raise WorkspacePathError("path 不能包含空字节")

    parts: list[str] = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise WorkspacePathError("path 不能包含 .. 段")
        if segment.startswith("~"):
            # WHY 拒绝 ~：它依赖运行进程的 HOME，同一个请求在不同部署下会指向
            # 不同位置——工作区路径没有任何理由需要这种环境相关的语义。
            raise WorkspacePathError("path 不能以 ~ 开头")
        if ":" in segment:
            # WHY 拒绝冒号：Windows 上 ``C:`` 是盘符、``file.txt:stream`` 是数据流，
            # 两者都能让「拼接后的路径」落到工作区之外或读到非预期内容。
            raise WorkspacePathError(f"path 片段不能包含冒号：{segment}")
        parts.append(segment)

    return "/" + "/".join(parts)


def _matched_mount(
    normalized: str, mounts: Mapping[str, Path] | None
) -> tuple[str, Path] | None:
    """按**最长前缀**找出这条虚拟路径落在哪个挂载点里；没有命中时返回 ``None``。

    WHY 最长匹配：挂载前缀可以嵌套，取短的那个会把子挂载里的文件解析到父挂载点的错误
    位置——而两边「都存在」，于是错误只会在读内容时才暴露。

    Args:
        normalized: 已归一化的虚拟路径（``/skills/code-review/SKILL.md``）。
        mounts: ``{虚拟前缀: 宿主目录}``；``None`` 表示没有挂载。

    Returns:
        ``(虚拟前缀去掉尾斜杠的写法, 宿主目录)``，或 ``None``。
    """
    best: tuple[str, Path] | None = None
    for prefix, host_dir in (mounts or {}).items():
        virtual = "/" + str(prefix).strip("/")
        if normalized == virtual or normalized.startswith(virtual + "/"):
            if best is None or len(virtual) > len(best[0]):
                best = (virtual, Path(host_dir))
    return best


def resolve_in_workspace(
    root: Path, virtual_path: str, *, mounts: Mapping[str, Path] | None = None
) -> Path:
    """把虚拟路径解析为宿主上的绝对路径（工作区内，或某个**只读挂载点**内）。

    WHY 必须在 ``resolve()`` 之后复检：字符串级拦截挡不住符号链接 / 目录联接——
    ``/link/secret`` 里没有 ``..``，字面上完全合法，但 ``link`` 指向工作区之外时
    真实落点已经跑出去了。只有解析出真实路径再比较前缀才能发现。

    WHY 要认挂载表（2026-09-21 改）：技能库、技能视图与工具输出留存已经搬到工作区之外，
    由只读挂载暴露在 ``/skills``、``/.skills-active``、``/_tool_outputs`` 下。面板要能打开
    「完整输出」（``/_tool_outputs/<会话 ID>/0001-execute.txt``），就得先按挂载表把那一条
    还原成宿主路径——否则它会被当成工作区内的相对路径，读到一个不存在的文件。

    Args:
        root: 工作区根目录（不含虚拟路径）。
        virtual_path: 以 ``/`` 开头的虚拟路径。
        mounts: ``{虚拟前缀: 宿主目录}``；``None`` 表示只认工作区。

    Returns:
        宿主绝对路径（工作区内，或挂载点内）。路径**不要求存在**：列目录与读文件各自
        负责把「不存在」映射成 404。

    Raises:
        WorkspacePathError: 路径非法，或解析后逃出了它应当在的那个根。
    """
    if root is None:
        raise WorkspacePathError("工作区根目录不能为空")

    normalized = _normalize_virtual_path(virtual_path)
    matched = _matched_mount(normalized, mounts)
    if matched is None:
        base = Path(root).resolve()
        relative = normalized.lstrip("/")
    else:
        virtual, host_dir = matched
        base = host_dir.resolve()
        relative = normalized[len(virtual) :].lstrip("/")

    # WHY 显式拦盘符：``Path("ws") / "C:/x"`` 在 Windows 上会**丢弃**左侧基目录，
    # 直接得到 ``C:\\x``——这是拼接式实现最容易漏掉的一条逃逸路径。
    if relative and PureWindowsPath(relative).drive:
        raise WorkspacePathError(f"path 不能包含盘符：{virtual_path}")

    candidate = (base / relative).resolve() if relative else base

    if not candidate.is_relative_to(base):
        # 报出不逃逸的那一侧，避免把宿主的绝对路径回显给调用方
        scope = "挂载点" if matched is not None else "工作区"
        raise WorkspacePathError(f"path 逃出{scope}：{virtual_path}")

    return candidate


def to_virtual_path(root: Path, target: Path) -> str:
    """把工作区内的绝对路径换算回虚拟路径。"""
    resolved_root = Path(root).resolve()
    relative = Path(target).resolve().relative_to(resolved_root)
    return "/" + relative.as_posix() if relative.as_posix() != "." else "/"


def _entry_of(child: Path, *, virtual_dir: str) -> WorkspaceEntry | None:
    """构造一条目录项；无法 ``stat`` 的条目返回 ``None``。

    WHY 用「所在目录的虚拟路径 + 子项名」拼、而不再从宿主路径反算（2026-09-21 改）：
    条目可能位于**挂载点**里（技能库 / 工具留存已经搬出工作区），从宿主路径反算需要先
    知道它在哪个挂载点下；而子项名本身就是虚拟路径的最后一段，拼出来永远正确。
    顺带避开一个更早的坑：反算要走 ``resolve()``，而指向根外的软链会让它抛异常，
    一次列举因此整条失败。
    """
    virtual = f"{virtual_dir.rstrip('/')}/{child.name}" if virtual_dir != "/" else f"/{child.name}"
    try:
        info = child.lstat()
    except OSError:
        # 断链的软链、权限不足的条目：跳过而不是让整个列举失败——
        # 一个不可读的条目不该让用户看不到同一目录下的其余文件。
        logger.debug("跳过无法读取的目录项：%s", child)
        return None

    is_link = child.is_symlink()
    size = info.st_size
    modified = info.st_mtime
    if is_link:
        # 软链自身的大小无意义（指向目标的路径长度），展示目标的大小更接近预期
        try:
            target_stat = child.stat()
            size = target_stat.st_size
            modified = target_stat.st_mtime
        except OSError:
            logger.debug("软链目标不可读：%s", child)

    return WorkspaceEntry(
        name=child.name,
        path=virtual,
        is_dir=child.is_dir(),
        size=size,
        modified_at=modified,
        is_symlink=is_link,
    )


def list_directory(
    root: Path,
    virtual_path: str,
    *,
    max_entries: int,
    mounts: Mapping[str, Path] | None = None,
) -> WorkspaceListing:
    """列出一级目录内容（懒加载：一次只列一层）。

    WHY 只列一层：工作区里会有 ``node_modules`` 这类成千上万条目的目录，
    递归列举既慢又会把一次刷新变成一次全盘扫描；前端按需展开下一层即可。

    Args:
        root: 工作区根目录。
        virtual_path: 目标目录的虚拟路径。
        max_entries: 单次返回的条目数上限。
        mounts: ``{虚拟前缀: 宿主目录}``；面板要能列进行挂载点里的目录时需要它。

    Returns:
        目录项按「目录在前、名称不区分大小写升序」排列。

    Raises:
        WorkspacePathError: 路径非法或逃出工作区 / 挂载点。
        NotADirectoryError: 目标存在但不是目录。
        FileNotFoundError: 目标不存在。
    """
    target = resolve_in_workspace(root, virtual_path, mounts=mounts)
    if not target.exists():
        raise FileNotFoundError(f"目录不存在：{virtual_path}")
    if not target.is_dir():
        raise NotADirectoryError(f"不是目录：{virtual_path}")

    normalized = _normalize_virtual_path(virtual_path)
    entries: list[WorkspaceEntry] = []
    truncated = False
    with os.scandir(target) as iterator:
        for child in iterator:
            if len(entries) >= max_entries:
                truncated = True
                break
            # WHY 用 scandir 的 DirEntry 转 Path：Windows 上 DirEntry 已带类型
            # 信息，省掉一次 stat 系统调用；而构造条目仍需 lstat（要大小与时间）。
            entry = _entry_of(Path(child.path), virtual_dir=normalized)
            if entry is None:
                continue
            # WHY 在**根层**跳过应用数据目录：它的存在不该出现在面板里（见
            # ``_HIDDEN_ROOT_ENTRIES`` 的 WHY）。深层同名目录不过滤——那可能是用户自己的。
            if normalized == "/" and entry.name in _HIDDEN_ROOT_ENTRIES:
                continue
            entries.append(entry)

    entries.sort(key=lambda item: (not item.is_dir, item.name.lower()))

    return WorkspaceListing(
        path=normalized,
        parent=None if normalized == "/" else "/".join(normalized.rstrip("/").split("/")[:-1]) or "/",
        entries=tuple(entries),
        truncated=truncated,
    )


def read_bytes_capped(path: Path, *, max_bytes: int) -> tuple[bytes, int, bool]:
    """按字节上限读取文件。

    WHY 按字节而不是字符设限：文件是字节流，字符数要先解码才知道，而「解码一个
    数 GB 的文件只为判断它超限」正是要避免的开销。

    Args:
        path: 目标文件。
        max_bytes: 读取上限。

    Returns:
        ``(内容, 文件总大小, 是否被截断)``。
    """
    total = path.stat().st_size
    with path.open("rb") as handle:
        data = handle.read(max_bytes)
    return data, total, total > len(data)


def looks_binary(data: bytes) -> bool:
    """按内容判断是否为二进制。

    WHY 看空字节而不是看扩展名：没有后缀的文本文件（``Makefile``、``LICENSE``）
    用扩展名判会被当成二进制而不可预览；而真实文本里出现 ``\\x00`` 的概率极低。
    """
    return b"\x00" in data[:8192]


__all__ = [
    "IMAGE_SUFFIXES",
    "TEXT_SUFFIXES",
    "WorkspaceEntry",
    "WorkspaceListing",
    "WorkspacePathError",
    "list_directory",
    "looks_binary",
    "read_bytes_capped",
    "resolve_in_workspace",
    "to_virtual_path",
]
