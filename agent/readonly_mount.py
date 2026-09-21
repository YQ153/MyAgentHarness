"""把**根外的路径**只读地挂进 Agent 的虚拟文件系统：两种挂载，一条底线。

## 两种挂载（安全边界不同，因此不合成一个类）

- :class:`ReadOnlyFileMount`：只暴露**一个文件**（用于全局长期记忆 ``MEMORY_FILE``）。
  它的宿主父目录里可能是**用户的整个项目**，所以连 ``ls`` 都只列那一条——父目录不可见。
- :class:`ReadOnlyDirectoryMount`：暴露**一棵子树**（用于技能库、技能视图、工具输出
  留存）。宿主目录是我们自己的存储目录，本来就该整棵可读，只需把写一律拒掉。

## WHY 需要挂载这件事本身

Agent 的文件根是「本会话的工作区」，backend 只认虚拟路径——而有些路径天然在根外，却必须
被 Agent 读到：

- **不挂载**：deepagents 对读不到的来源是**静默跳过**的（memory 少一条、skills 一个都
  加载不到，两者都只留一行 WARNING）。「我配了/写了，Agent 却不照做」这类问题因此没有
  任何错误可循——旧实现正是这样（全局记忆在根外就被整条跳过）。
- **复制进根**：立刻有了第二份真相，人工改了源文件而副本没变——同样不报错。
- **让 backend 放行任意绝对路径**：等于拆掉「Agent 能碰宿主的哪片目录」这条边界，
  Web 与多租户场景下就是任意文件读取。

## 两条共同的底线

1. **只读**：写、改、删、上传一律拒绝。这不是「靠上游权限规则兜」——``sandbox`` 档位下
   工具级路径规则已被停用（execute 走 shell，路径规则拦不住），挂载点自己就是那道边界。
2. **越界必须由本模块判**：路径先归一化，含 ``..`` 或不是绝对路径一律判非法；不做
   「回退一级」的路径运算。需要回溯的路径就不是挂载点里的东西。

## 为什么 ``grep`` 不委托给上游（单文件挂载）

``FilesystemBackend.grep(pattern, path=<一个文件>)`` 实测会搜**整个父目录**（传
``/AGENTS.md`` 却匹配到了同目录的另一个文件）。委托它等于把父目录暴露出去，因此那里自己
扫这一个文件。
"""

from __future__ import annotations

import fnmatch
import logging
from datetime import datetime
from pathlib import Path

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from agent.path_safety import ExtendedPathSafeBackendMixin

logger = logging.getLogger(__name__)

_FILE_NOT_FOUND = "file_not_found"
"""上游约定的可恢复错误码：memory 中间件见到它会把这条来源**跳过**而不是当作故障。

WHY 必须逐字对齐：``MemoryMiddleware.before_agent`` 用的是 ``== "file_not_found"``
而不是包含判断，改一个字就会让「文件没了」从「少一条记忆」升级成「整轮运行报错」。
"""

_NOT_A_DIRECTORY = "not_a_directory"
"""上游约定的「这不是目录」错误码，用于 ``ls`` 指向文件的情形。"""


class ReadOnlyFileMount(BackendProtocol):
    """虚拟文件系统里一个只读的单文件挂载点。

    Attributes:
        host_path: 宿主上的真实文件。
        label: 出现在错误文案里的说法（如「全局长期记忆」），也用于日志。
    """

    def __init__(self, host_path: Path, *, label: str, mounted_at: str | None = None) -> None:
        """构造挂载点。

        Args:
            host_path: 要暴露的宿主文件（绝对路径由调用方保证；这里只要求它有文件名）。
            label: 人类可读的说明，用于错误文案与日志。
            mounted_at: 这个文件**对外**的完整虚拟路径（如 ``/global/AGENTS.md``）；
                只影响错误文案，``None`` 表示与内部路径相同。

        Raises:
            ValueError: ``host_path`` 为 ``None`` 或没有文件名、``label`` 为空、
                ``mounted_at`` 给了但不是以 ``/`` 开头的路径。

        Note:
            WHY 不在这里校验文件存在：装配发生在请求路径上，而文件可能刚好在这一刻被
            人工删掉/替换（它本来就是人工维护的）。此时正确的行为是「读的时候报
            ``file_not_found``」（中间件会跳过这条来源），而不是让整次装配失败。

            WHY 要 ``mounted_at``：``CompositeBackend`` 按前缀路由时会**剥掉**前缀再转发，
            所以本对象内部看到的永远是 ``/AGENTS.md``。但错误文案是给模型看的，而模型
            刚才写的路径是 ``/global/AGENTS.md``——回它一个 ``/AGENTS.md`` 会让它以为
            「那是工作区里的另一个文件」，于是换一种写法再试一次。
        """
        if host_path is None:
            raise ValueError("host_path 不能为 None")
        if not label or not str(label).strip():
            raise ValueError("label 不能为空：错误文案要靠它说明这是哪一种挂载")
        if mounted_at is not None and (
            not isinstance(mounted_at, str) or not mounted_at.startswith("/")
        ):
            raise ValueError(f"mounted_at 必须是以 / 开头的虚拟路径，实际：{mounted_at!r}")

        host = host_path
        if not getattr(host, "name", ""):
            raise ValueError(f"host_path 必须指向一个文件（有文件名）：{host}")

        self._host = host
        self._label = str(label)
        self._name = host.name
        self._virtual_path = f"/{host.name}"
        self._mounted_at = mounted_at or self._virtual_path
        # WHY 根取父目录：read/download 直接复用上游对编码、行号与分页的处理，
        # 自己再实现一遍必然与「文件工具看到的格式」不一致（模型会看到两种行号口径）。
        # 父目录里其它文件由本类的方法白名单挡住，见模块 docstring 的安全边界。
        self._inner = FilesystemBackend(root_dir=str(host.parent), virtual_mode=True)

    # ------------------------------------------------------------------ 观测

    @property
    def host_path(self) -> Path:
        """宿主上的真实文件。"""
        return self._host

    @property
    def virtual_path(self) -> str:
        """本挂载点内部看到的路径（路由前缀由 ``CompositeBackend`` 负责）。

        注意：读取结果里的 ``path`` 用它，因为 ``CompositeBackend`` 会按前缀把它重映射
        回对外路径；错误文案用 :attr:`mounted_at`，因为那是模型写下的路径。
        """
        return self._virtual_path

    @property
    def mounted_at(self) -> str:
        """这个文件对外的完整虚拟路径（错误文案用）。"""
        return self._mounted_at

    @property
    def label(self) -> str:
        """人类可读的说明。"""
        return self._label

    def describe(self) -> str:
        """一行说明，供装配日志使用。"""
        return f"只读挂载 {self._mounted_at} ← {self._host}（{self._label}）"

    # ------------------------------------------------------------------ 路径白名单

    def _segments(self, path: object) -> list[str] | None:
        """把请求路径归一化成段列表；不是绝对路径或含 ``..`` 时返回 ``None``。

        WHY ``..`` 直接判非法而不是消掉它：挂载点里只有一层、没有目录可回溯，任何需要
        回溯的路径都不是它里面的东西。放行后再判，等于让「是否越界」变成一段需要推理的
        路径运算——那段推理一旦有偏差，暴露的是父目录。
        """
        if not isinstance(path, str) or not path.startswith("/"):
            return None
        parts = path.split("/")
        if any(part == ".." for part in parts):
            return None
        return [part for part in parts if part not in ("", ".")]

    def _wants_mount_root(self, path: object) -> bool:
        """请求的是不是挂载点自身（``/``）。"""
        return self._segments(path) == []

    def _wants_the_file(self, path: object) -> bool:
        """请求的是不是挂载的那个文件。"""
        return self._segments(path) == [self._name]

    def _not_found(self, path: object) -> str:
        """统一的「不在这里」文案：带上挂载点里到底有什么，模型才知道下一步该做什么。"""
        return f"Path '{path}': {_FILE_NOT_FOUND}（{self._label}只挂载了 {self._mounted_at}）"

    def _readonly(self, action: str) -> str:
        """统一的「只读」文案。

        WHY 用 ``mounted_at`` 而不是内部路径：这句话是回给模型的，而它上一步写的路径是
        对外那个（``/global/AGENTS.md``）。回一个 ``/AGENTS.md``，它会以为那是工作区里的
        另一个同名文件，于是换一种写法再试。
        """
        return (
            f"{self._label}是只读的（由人工维护），不支持{action}：{self._mounted_at}。"
            "要修改它请在宿主机上直接编辑源文件。"
        )

    # ------------------------------------------------------------------ 目录

    def ls(self, path: str) -> LsResult:
        """列出挂载点内容：只有那一个文件。

        WHY 自己拼条目而不是把父目录的列举结果过滤一遍：父目录可能是任意目录（用户把
        记忆文件放在项目里也完全正常），列一遍既慢又让「哪些文件曾进入过本进程」成为
        可观测事实。这里只 ``stat`` 那一个文件。

        Args:
            path: 挂载点内的路径；只接受 ``/``。

        Returns:
            ``LsResult``；路径不是挂载点根时返回错误（文案与上游 ``not_a_directory`` 对齐）。
        """
        if not self._wants_mount_root(path):
            return LsResult(error=f"Path '{path}': {_NOT_A_DIRECTORY}")
        entry = self._entry()
        return LsResult(entries=[entry] if entry is not None else [])

    def _entry(self) -> FileInfo | None:
        """挂载文件的条目；文件此刻不存在时返回 ``None``（列举为空是如实结果）。"""
        try:
            stat = self._host.stat()
        except OSError as exc:
            logger.warning("只读挂载点列举失败：%s（%s）", self._host, exc)
            return None
        return FileInfo(
            path=self._virtual_path,
            is_dir=False,
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(),
        )

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """在挂载点内匹配文件：它只可能匹配到那一个文件。

        WHY 用 ``fnmatch`` 近似而不是复用上游的 glob：上游的 glob 是「从某个根走整棵树」，
        而这里只有一层、一个文件。近似规则的误差方向是安全的——多判一次「匹配」只是把
        调用方本来就指着的那个文件还给它；少判一次「匹配」最多让某个古怪写法搜不到它。
        真正危险的方向（匹配到父目录里的别的文件）在这里不可能发生。

        Args:
            pattern: glob 模式（上游可能带路由前缀，如 ``/**/*.md``）。
            path: 搜索起点；``None`` 或 ``/`` 表示整个挂载点。

        Returns:
            ``GlobResult``；挂载点里没有别的路径，因此越界路径返回「没有匹配」而不是错误。
        """
        if not isinstance(pattern, str) or not pattern.strip():
            return GlobResult(matches=[], error="pattern 必须是非空字符串")
        if path is not None and not (self._wants_mount_root(path) or self._wants_the_file(path)):
            return GlobResult(matches=[])
        entry = self._entry()
        if entry is None:
            return GlobResult(matches=[])
        return GlobResult(matches=[entry] if self._matches_glob(pattern) else [])

    def _matches_glob(self, pattern: str) -> bool:
        """把 glob 模式折算到「只有一层」的场景再匹配文件名。

        折算是**保守**的：只剥掉开头的 ``/`` 与 ``**/``（它们在单层挂载点里没有意义），
        其余原样交给 :func:`fnmatch.fnmatch`。
        """
        candidate = pattern.lstrip("/")
        while candidate.startswith("**/"):
            candidate = candidate[3:]
        return fnmatch.fnmatch(self._name, candidate) or fnmatch.fnmatch(
            self._virtual_path.lstrip("/"), candidate
        )

    # ------------------------------------------------------------------ 读

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """读挂载文件的一段。

        Args:
            file_path: 挂载点内的路径；只接受那一个文件。
            offset: 起始行（0 基）。
            limit: 最多返回多少行。

        Returns:
            ``ReadResult``；路径不在挂载点内时 ``error`` 里带 ``file_not_found``。
        """
        if not self._wants_the_file(file_path):
            return ReadResult(error=self._not_found(file_path))
        # 分页参数先在这里挡一道：上游对非法取值会直接抛异常，而工具层抛异常会打断
        # 整轮图执行——那本可以是模型自己改一个参数就能继续的事。
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return ReadResult(error=f"offset 必须是非负整数，实际：{offset!r}")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            return ReadResult(error=f"limit 必须是非负整数，实际：{limit!r}")
        return self._inner.read(self._virtual_path, offset=offset, limit=limit)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """按批取文件内容（deepagents 的 ``MemoryMiddleware`` 走的就是这个接口）。

        Args:
            paths: 要下载的路径列表。

        Returns:
            与 ``paths`` 等长、顺序一致的响应列表；不在挂载点内的路径给出
            ``file_not_found``（而不是 ``None`` 或异常）——中间件据此跳过该来源。

        Raises:
            ValueError: ``paths`` 不是列表。
        """
        if not isinstance(paths, list):
            raise ValueError(f"paths 必须是列表，实际：{type(paths).__name__}")
        results: list[FileDownloadResponse] = []
        for path in paths:
            if not self._wants_the_file(path):
                results.append(
                    FileDownloadResponse(path=str(path), content=None, error=_FILE_NOT_FOUND)
                )
                continue
            downloaded = self._inner.download_files([self._virtual_path])[0]
            # WHY 重新构造响应：上游返回的 ``path`` 是挂载点内部的写法，而调用方要按
            # 自己给出的路径对号入座（``CompositeBackend`` 也是这么做的）。
            results.append(
                FileDownloadResponse(
                    path=str(path), content=downloaded.content, error=downloaded.error
                )
            )
        return results

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """在挂载文件里做**字面量**搜索。

        Args:
            pattern: 要找的字面量文本（不是正则，与上游口径一致）。
            path: 搜索范围；``None`` 或 ``/`` 或文件本身都表示「只搜这一个文件」。
            glob: 文件名过滤；不匹配时直接返回空结果。
            max_count: 命中数上限；超出时截断并标注 ``truncated``。

        Returns:
            ``GrepResult``；命中项的 ``path`` 用挂载点内部的写法，由调用方按需加前缀。

        Raises:
            ValueError: ``max_count`` 给了但不是正整数。
        """
        if not isinstance(pattern, str) or not pattern:
            return GrepResult(matches=[], error="pattern 必须是非空字符串")
        if max_count is not None and (
            not isinstance(max_count, int) or isinstance(max_count, bool) or max_count <= 0
        ):
            raise ValueError(f"max_count 必须是正整数，实际：{max_count!r}")
        if path is not None and not (self._wants_mount_root(path) or self._wants_the_file(path)):
            return GrepResult(matches=[])
        if glob and not self._matches_glob(glob):
            return GrepResult(matches=[])

        try:
            text = self._host.read_text(encoding="utf-8")
        except FileNotFoundError:
            return GrepResult(matches=[], error=self._not_found(path or self._virtual_path))
        except (OSError, UnicodeDecodeError) as exc:
            # 记忆文件按约定是 UTF-8 文本；读不到就如实说原因，而不是当成「没有匹配」——
            # 后者会让「记忆里明明写了这条」变成一次无法解释的检索失败。
            logger.warning("只读挂载点检索失败：%s（%s）", self._host, exc)
            return GrepResult(matches=[], error=f"读取失败：{exc}")

        matches: list[GrepMatch] = [
            GrepMatch(path=self._virtual_path, line=number, text=line)
            for number, line in enumerate(text.splitlines(), start=1)
            if pattern in line
        ]
        if max_count is not None and len(matches) > max_count:
            return GrepResult(matches=matches[:max_count], truncated=True)
        return GrepResult(matches=matches)

    # ------------------------------------------------------------------ 写（一律拒绝）

    def write(self, file_path: str, content: str) -> WriteResult:
        """拒绝写入（只读挂载点）。"""
        logger.warning("拒绝写入只读挂载点：%s（%s）", file_path, self._label)
        return WriteResult(error=self._readonly("写入"))

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """拒绝就地修改（只读挂载点）。"""
        logger.warning("拒绝修改只读挂载点：%s（%s）", file_path, self._label)
        return EditResult(error=self._readonly("修改"))

    def delete(self, file_path: str) -> DeleteResult:
        """拒绝删除（只读挂载点）。"""
        logger.warning("拒绝删除只读挂载点：%s（%s）", file_path, self._label)
        return DeleteResult(error=self._readonly("删除"))

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """拒绝上传覆盖（只读挂载点）。

        Args:
            files: ``(路径, 内容)`` 列表。

        Returns:
            与入参等长、顺序一致的响应列表，每项都带只读说明。

        Raises:
            ValueError: ``files`` 不是列表。
        """
        if not isinstance(files, list):
            raise ValueError(f"files 必须是列表，实际：{type(files).__name__}")
        message = self._readonly("覆盖")
        logger.warning("拒绝上传到只读挂载点：%d 个文件（%s）", len(files), self._label)
        return [
            FileUploadResponse(path=str(entry[0]) if entry else "", error=message) for entry in files
        ]


class _PrefixTolerantFilesystemBackend(ExtendedPathSafeBackendMixin, FilesystemBackend):
    """目录挂载用的内部后端：混入 Windows 扩展前缀容错。

    WHY 不直接用 ``agent/backends.py`` 里那个同类：``backends`` 必须 import 本模块
    （它负责把挂载接进 ``CompositeBackend``），反过来 import 会成环。两者的差别只有
    「混了哪个 mixin」，而那份 mixin 只有一处定义（``agent/path_safety.py``）。
    """


class ReadOnlyDirectoryMount(BackendProtocol):
    """虚拟文件系统里一棵**只读**的目录树。

    WHY 与 :class:`ReadOnlyFileMount` 并存、而不合成一个：
    单文件挂载的宿主父目录里可能是**用户的整个项目**，所以它必须只暴露一个文件（连
    ``ls`` 都只列一条）；而目录挂载面对的是**我们自己的存储目录**（技能库 / 技能视图 /
    工具留存），本来就该整棵可读。两者的安全边界不同，合成一个只会让「父目录不可见」
    这条规则在两类场景之间互相妥协。

    WHY 写必须被拒（而不是「交给上游权限规则」）：这三个位置都不是 Agent 该写的——
    技能库由人工维护、技能视图是派生产物（写它等于篡改物化结果）、工具留存由服务端自己
    落盘。更实际的一条：``sandbox`` 档位下工具级文件权限规则已被停用（execute 走 shell，
    路径规则拦不住），此时挂载点自己就是唯一那道边界。

    Attributes:
        host_dir: 宿主上的真实目录。
        label: 出现在错误文案里的说法（如「技能库」），也用于日志。
    """

    def __init__(self, host_dir: Path, *, label: str) -> None:
        """构造目录挂载点。

        Args:
            host_dir: 要暴露的宿主目录（绝对路径由调用方保证）。
            label: 人类可读的说明，用于错误文案与日志。

        Raises:
            ValueError: ``host_dir`` 为 ``None`` 或不是 ``Path``、``label`` 为空。

        Note:
            WHY 不在这里校验目录存在：装配发生在请求路径上，而存储目录由
            ``SessionRoot.ensure_storage`` 按需创建——比「拒绝装配」更合适的是让读操作
            如实返回 ``file_not_found`` / ``not_a_directory``。
        """
        if host_dir is None:
            raise ValueError("host_dir 不能为 None")
        if not isinstance(host_dir, Path):
            raise ValueError(f"host_dir 必须是 Path，实际：{type(host_dir).__name__}")
        if not label or not str(label).strip():
            raise ValueError("label 不能为空：错误文案要靠它说明这是哪一种挂载")

        self._host = host_dir
        self._label = str(label)
        # 内部后端就是「以这个目录为根」的普通文件系统后端：路径越界、盘符、``..`` 这些
        # 判断它已经有一套（含 Windows 前缀容错），这里只负责把写操作拿掉。
        self._inner = _PrefixTolerantFilesystemBackend(root_dir=str(host_dir))

    # ------------------------------------------------------------------ 观测

    @property
    def host_dir(self) -> Path:
        """宿主上的真实目录。"""
        return self._host

    @property
    def label(self) -> str:
        """人类可读的说明。"""
        return self._label

    def describe(self) -> str:
        """一行说明，供装配日志使用。"""
        return f"只读挂载 {self._host}（{self._label}）"

    # ------------------------------------------------------------------ 读（委托）

    def ls(self, path: str) -> LsResult:
        """列出目录内容（只读委托）。"""
        try:
            return self._inner.ls(path)
        except (ValueError, OSError) as exc:
            return LsResult(error=self._rejected("列目录", path, exc))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """读一个文件（只读委托）。"""
        try:
            return self._inner.read(file_path, offset=offset, limit=limit)
        except (ValueError, OSError) as exc:
            return ReadResult(error=self._rejected("读取", file_path, exc))

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """按字面量检索（只读委托）。"""
        try:
            return self._inner.grep(pattern, path, glob, max_count=max_count)
        except (ValueError, OSError) as exc:
            return GrepResult(matches=[], error=self._rejected("检索", path or pattern, exc))

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """按模式匹配文件（只读委托）。"""
        try:
            return self._inner.glob(pattern, path)
        except (ValueError, OSError) as exc:
            return GlobResult(matches=[], error=self._rejected("匹配", path or pattern, exc))

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """按批取内容（只读委托）。

        Raises:
            ValueError: ``paths`` 不是列表。
        """
        if not isinstance(paths, list):
            raise ValueError(f"paths 必须是列表，实际：{type(paths).__name__}")
        try:
            return self._inner.download_files(paths)
        except (ValueError, OSError) as exc:
            message = self._rejected("读取", paths, exc)
            return [
                FileDownloadResponse(path=str(item), content=None, error=message) for item in paths
            ]

    def _rejected(self, action: str, path: object, exc: Exception) -> str:
        """把上游的路径类异常压成一句可读的失败说明。

        WHY 不让它抛出去：工具层抛异常会打断整轮图执行，而「路径写错了」本可以是模型自己
        改一个参数就能继续的事（实测上游对 ``..`` 段直接抛 ``ValueError: Path traversal not
        allowed``，对指向目录的读也抛异常）。
        """
        logger.warning(
            "只读挂载点拒绝%s：%s（%s：%s）", action, path, type(exc).__name__, exc
        )
        return f"{action}失败：Path '{path}' 不在本挂载点内（{self._label}）"

    # ------------------------------------------------------------------ 写（一律拒绝）

    def _readonly(self, action: str) -> str:
        """统一的「只读」文案。"""
        return (
            f"{self._label}是只读的，不支持{action}：{self._host}。"
            "技能库由人工维护；技能视图与工具留存由服务端生成。"
        )

    def write(self, file_path: str, content: str) -> WriteResult:
        """拒绝写入。"""
        logger.warning("拒绝写入只读挂载点：%s（%s）", file_path, self._label)
        return WriteResult(error=self._readonly("写入"))

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """拒绝就地修改。"""
        logger.warning("拒绝修改只读挂载点：%s（%s）", file_path, self._label)
        return EditResult(error=self._readonly("修改"))

    def delete(self, file_path: str) -> DeleteResult:
        """拒绝删除。"""
        logger.warning("拒绝删除只读挂载点：%s（%s）", file_path, self._label)
        return DeleteResult(error=self._readonly("删除"))

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """拒绝上传覆盖。

        Raises:
            ValueError: ``files`` 不是列表。
        """
        if not isinstance(files, list):
            raise ValueError(f"files 必须是列表，实际：{type(files).__name__}")
        message = self._readonly("覆盖")
        logger.warning("拒绝上传到只读挂载点：%d 个文件（%s）", len(files), self._label)
        return [
            FileUploadResponse(path=str(entry[0]) if entry else "", error=message) for entry in files
        ]
