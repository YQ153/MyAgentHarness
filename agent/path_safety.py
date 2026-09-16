"""Windows 扩展路径前缀（``\\\\?\\``）容错。

背景（2026-09-16 会话 8ea43053 故障复盘）：

模型在一条 AIMessage 中并行发出 6 个 ``write_file``，目标目录
``/react-vite-app/`` 当时尚不存在。``FilesystemBackend.write`` 的顺序是
「先 ``_resolve_path``（内部 ``Path.resolve()``）再 ``mkdir``」，于是出现
如下竞态（CPython ``ntpath.realpath`` 的已知缺陷）：

1. 线程 A 解析 ``/react-vite-app/eslint.config.js`` 时目录还不存在，
   ``_getfinalpathname`` 返回 winerror 3（ERROR_PATH_NOT_FOUND），
   realpath 走非严格回退，把无法解析的尾巴拼到 ``\\\\?\\`` 前缀的
   最近可解析祖先上；
2. 线程 B 的 ``mkdir`` 在毫秒后建好目录；线程 A 收尾时对「剥掉前缀的
   路径」做二次探测，此时得到 winerror 2（ERROR_FILE_NOT_FOUND）；
3. ``ntpath.realpath`` 只有在 ``二次探测错误码 == 初始错误码`` 时才剥前缀
   （``if ex.winerror == initial_winerror: path = spath``），2 != 3，
   前缀被保留，``Path.resolve()`` 返回
   ``\\\\?\\C:\\...\\eslint.config.js``；
4. ``FilesystemBackend._resolve_path`` 里 ``full.relative_to(self.cwd)``
   对带前缀路径必然失败，抛出
   ``ValueError: Path:\\\\?\\... outside root directory``；
5. ``write()`` 只捕获 ``(OSError, RuntimeError)``，ValueError 直接炸穿
   工具任务、终止整轮会话。

本模块提供一个 backend Mixin：在越界校验前把 ``\\\\?\\`` /
``\\\\?\\UNC\\`` 前缀规范化掉。语义与 deepagents 官方实现保持一致
（穿越拦截、根目录越界拦截、symlink 环检测），仅消除前缀导致的误报。
"""

from __future__ import annotations

import errno
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deepagents.backends import FilesystemBackend

logger = logging.getLogger(__name__)

_EXTENDED_PREFIX = "\\\\?\\"
"""Windows 扩展长度路径前缀（verbatim），如 ``\\\\?\\C:\\a\\b``。"""

_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"
"""UNC 形式的扩展前缀，去掉前缀后需还原为 ``\\\\server\\share``。"""


def strip_extended_prefix(path: Path) -> Path:
    """剥掉 Windows 扩展长度路径前缀，非前缀路径原样返回。

    WHY 必须在越界校验前做：``Path.relative_to`` / ``is_relative_to`` 按
    字符串逐组件比较，``\\\\?\\C:\\a`` 与 ``C:\\a`` 永不相等；两种形式
    指向同一磁盘位置，前缀差异只是 ``GetFinalPathNameByHandle`` 的输出
    形态，不代表越界。

    Args:
        path: 任意 ``Path``，可能带扩展前缀。

    Returns:
        等价的不带前缀路径；输入无前缀时返回原对象（零拷贝语义）。

    Raises:
        ValueError: 输入为 ``None``。
    """
    if path is None:
        raise ValueError("path 不能为 None")

    raw = str(path)
    if raw.startswith(_EXTENDED_UNC_PREFIX):
        return Path("\\\\" + raw[len(_EXTENDED_UNC_PREFIX) :])
    if raw.startswith(_EXTENDED_PREFIX):
        return Path(raw[len(_EXTENDED_PREFIX) :])
    return path


try:  # pragma: no cover - 版本探测分支，取决于安装的 deepagents
    from deepagents.backends.filesystem import _raise_if_symlink_loop as _symlink_loop_check
except ImportError:  # pragma: no cover - 上游重命名私有函数时的兜底

    def _symlink_loop_check(path: Path) -> None:
        """上游私有函数缺失时的降级实现：只识别 POSIX ELOOP。

        WHY 打日志而不是静默跳过：symlink 环检测是 ``_resolve_path`` 的
        安全语义之一，降级实现覆盖面变窄（Windows 的 winerror=1921
        不在识别范围内），必须让运维能从日志发现这层退化。
        """
        logger.warning(
            "deepagents 私有符号 _raise_if_symlink_loop 不可用，"
            "path_safety 使用降级的 symlink 环检测（仅识别 errno.ELOOP）"
        )
        if not path.is_symlink():
            return
        try:
            path.stat()
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise


class ExtendedPathSafeBackendMixin:
    """在 ``FilesystemBackend`` 及其子类前混入，消除 ``\\\\?\\`` 前缀误报。

    必须放在 MRO 首位（``class X(ExtendedPathSafeBackendMixin, LocalShellBackend)``），
    以确保本 Mixin 的 ``_resolve_path`` 优先于官方实现被调用；其余方法
    全部沿用官方实现，不改变任何其他行为。
    """

    def _resolve_path(self, key: str) -> Path:
        """解析路径并校验越界，官方实现误报时按规范化路径重判。

        策略：先完整走一遍官方实现（成功则零漂移返回）；只有官方抛
        ``ValueError`` 时才进入重判分支——重判复刻官方的穿越拦截与
        越界校验，差异仅在比较前剥掉 ``\\\\?\\`` 前缀。

        Args:
            key: 工具传入的文件路径（虚拟模式下来自模型，任意形态）。

        Returns:
            位于 ``self.cwd`` 之内的绝对 ``Path``。

        Raises:
            ValueError: 路径含 ``..`` / ``~`` 穿越，或规范化后确实在
                根目录之外。
            OSError: symlink 环（ELOOP）。
        """
        backend: FilesystemBackend = self  # type: ignore[assignment]
        try:
            return super()._resolve_path(key)  # type: ignore[misc]
        except ValueError:
            # 两种可能：真实越界/穿越，或 \\?\ 前缀误报。先复刻官方的
            # 穿越拦截，保证恶意输入不可能因为走了重判分支而放宽。
            if not getattr(backend, "virtual_mode", False):
                # 非虚拟模式：官方实现不会因前缀误报（绝对路径原样返回），
                # 抛出的 ValueError 只能是真实问题，直接向上传播。
                raise
            vpath = key if key.startswith("/") else "/" + key
            if ".." in vpath or vpath.startswith("~"):
                raise ValueError("Path traversal not allowed") from None

            candidate = (backend.cwd / vpath.lstrip("/")).resolve()
            normalized = strip_extended_prefix(candidate)
            try:
                normalized.relative_to(backend.cwd)
            except ValueError:
                msg = f"Path:{normalized} outside root directory: {backend.cwd}"
                raise ValueError(msg) from None
            _symlink_loop_check(normalized)
            logger.info(
                "path_safety 规范化扩展前缀路径：%s -> %s（key=%r）",
                candidate,
                normalized,
                key,
            )
            return normalized

    def _to_virtual_path(self, path: Path) -> str:
        """把宿主路径转换为虚拟路径，容忍 ``resolve()`` 返回的前缀形态。

        官方实现的调用方（``_display_path`` 等）对这里的 ``ValueError``
        有「回退到裸文件名」的兜底，但那会让 ``ls`` / ``glob`` 结果退化；
        前缀误报直接消除掉，兜底留给真正的越界路径。
        """
        backend: FilesystemBackend = self  # type: ignore[assignment]
        return "/" + strip_extended_prefix(path.resolve()).relative_to(backend.cwd).as_posix()
