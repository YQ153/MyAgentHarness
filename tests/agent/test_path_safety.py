"""``agent.path_safety`` 回归测试。

核心场景来自 2026-09-16 会话 8ea43053 故障：并行 ``write_file`` 新建目录时
``Path.resolve()`` 返回 ``\\\\?\\`` 前缀路径，官方越界校验误报导致整轮会话
崩溃。这里用「父目录尚不存在时 resolve 返回前缀路径」的替身确定性复现该
竞态，验证 Mixin 修复后行为正确、且安全校验（穿越 / 越界）未被放宽。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from deepagents.backends import FilesystemBackend

from agent.path_safety import ExtendedPathSafeBackendMixin, strip_extended_prefix

_real_resolve = Path.resolve
"""打桩前保存的原始 ``Path.resolve``，替身内部仍需真实解析结果。"""


def _race_simulating_resolve(self: Path, strict: bool = False) -> Path:  # noqa: FBT001, FBT002
    """复刻 CPython ``ntpath.realpath`` 在并发建目录下的缺陷形态。

    规则：父目录尚不存在时，返回 ``\\\\?\\`` 前缀路径（对应「初次探测
    winerror 3、二次探测 winerror 2，前缀被保留」的真实行为）；父目录已
    存在时返回正常路径（对应竞争中获胜的那批线程）。
    """
    real = _real_resolve(self, strict=strict)
    if not real.parent.exists():
        return Path("\\\\?\\" + str(real))
    return real


class _SafeBackend(ExtendedPathSafeBackendMixin, FilesystemBackend):
    """被测目标：与 agent/backends.py 中 disabled 档位同构。"""


@pytest.fixture
def race_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局打桩 ``Path.resolve``，模拟并发新建目录时的前缀竞态。"""
    monkeypatch.setattr(Path, "resolve", _race_simulating_resolve)


class TestStripExtendedPrefix:
    def test_verbatim_prefix_stripped(self) -> None:
        assert strip_extended_prefix(Path(r"\\?\C:\a\b.txt")) == Path(r"C:\a\b.txt")

    def test_unc_prefix_restored(self) -> None:
        assert strip_extended_prefix(Path(r"\\?\UNC\server\share\f")) == Path(r"\\server\share\f")

    def test_plain_path_untouched(self) -> None:
        plain = Path(r"C:\a\b.txt")
        assert strip_extended_prefix(plain) is plain

    def test_none_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能为 None"):
            strip_extended_prefix(None)  # type: ignore[arg-type]


class TestResolvePathUnderRace:
    def test_prefixed_resolve_no_longer_misfires(self, tmp_path: Path, race_resolve: None) -> None:
        """竞态形态下，官方实现误报、Mixin 修复后正常返回根内路径。"""
        root = tmp_path / "workspace"
        root.mkdir()
        backend = _SafeBackend(root_dir=str(root), virtual_mode=True)

        resolved = backend._resolve_path("/new-dir/eslint.config.js")

        assert not str(resolved).startswith("\\\\?\\")
        assert resolved == root / "new-dir" / "eslint.config.js"
        assert resolved.relative_to(root).as_posix() == "new-dir/eslint.config.js"

    def test_official_backend_still_fails_under_race(self, tmp_path: Path, race_resolve: None) -> None:
        """对照实验：同一竞态下未混入 Mixin 的官方后端复现原始故障。"""
        root = tmp_path / "workspace"
        root.mkdir()
        backend = FilesystemBackend(root_dir=str(root), virtual_mode=True)

        with pytest.raises(ValueError, match="outside root directory"):
            backend._resolve_path("/new-dir/eslint.config.js")

    def test_traversal_still_blocked(self, tmp_path: Path, race_resolve: None) -> None:
        """安全校验不得因修复而放宽：``..`` 穿越仍被拒绝。"""
        root = tmp_path / "workspace"
        root.mkdir()
        backend = _SafeBackend(root_dir=str(root), virtual_mode=True)

        with pytest.raises(ValueError, match="Path traversal not allowed"):
            backend._resolve_path("/../etc/passwd")

    def test_absolute_windows_path_outside_root_blocked(self, tmp_path: Path, race_resolve: None) -> None:
        """根外绝对路径（如 ``C:/Windows``）仍被拒绝。"""
        root = tmp_path / "workspace"
        root.mkdir()
        backend = _SafeBackend(root_dir=str(root), virtual_mode=True)

        with pytest.raises(ValueError, match="outside root directory"):
            backend._resolve_path("C:/Windows/system32/config.sys")

    def test_non_virtual_mode_propagates(self, tmp_path: Path) -> None:
        """非虚拟模式下 Mixin 不介入重判，官方行为原样透传。"""
        root = tmp_path / "workspace"
        root.mkdir()
        backend = _SafeBackend(root_dir=str(root), virtual_mode=False)

        # 非虚拟模式允许根外绝对路径（官方语义），Mixin 不改变该行为
        resolved = backend._resolve_path(str(tmp_path / "outside.txt"))
        assert resolved == tmp_path / "outside.txt"

    def test_mixin_passthrough_without_race(self, tmp_path: Path) -> None:
        """无竞态时（resolve 不带前缀）Mixin 与官方实现结果完全一致。"""
        root = tmp_path / "workspace"
        root.mkdir()
        (root / "sub").mkdir()
        (root / "sub" / "file.txt").write_text("x", encoding="utf-8")
        safe = _SafeBackend(root_dir=str(root), virtual_mode=True)
        official = FilesystemBackend(root_dir=str(root), virtual_mode=True)

        assert safe._resolve_path("/sub/file.txt") == official._resolve_path("/sub/file.txt")


class TestWriteUnderRace:
    def test_write_into_fresh_directory_succeeds(self, tmp_path: Path, race_resolve: None) -> None:
        """端到端：复现故障当夜的写入形态，修复后写入成功。"""
        root = tmp_path / "workspace"
        root.mkdir()
        backend = _SafeBackend(root_dir=str(root), virtual_mode=True)

        result = backend.write("/react-vite-app/eslint.config.js", "export default [];\n")

        assert result.error is None
        assert result.path == "/react-vite-app/eslint.config.js"
        assert (root / "react-vite-app" / "eslint.config.js").read_text(encoding="utf-8") == (
            "export default [];\n"
        )

    def test_parallel_writes_into_fresh_directory_all_succeed(
        self, tmp_path: Path, race_resolve: None
    ) -> None:
        """多线程并行写同一新目录：6 路并发全部成功（故障当晚 1/6 失败）。"""
        root = tmp_path / "workspace"
        root.mkdir()
        backend = _SafeBackend(root_dir=str(root), virtual_mode=True)
        names = [
            "index.html",
            "package.json",
            "vite.config.js",
            ".gitignore",
            "README.md",
            "eslint.config.js",
        ]
        errors: list[str] = []
        errors_lock = threading.Lock()

        def _write(name: str) -> None:
            result = backend.write(f"/react-vite-app/{name}", f"content of {name}\n")
            if result.error is not None:
                with errors_lock:
                    errors.append(f"{name}: {result.error}")

        threads = [threading.Thread(target=_write, args=(name,)) for name in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        for name in names:
            assert (root / "react-vite-app" / name).read_text(encoding="utf-8") == (
                f"content of {name}\n"
            )


class TestToVirtualPath:
    def test_prefixed_path_converted_without_error(self, tmp_path: Path) -> None:
        """带前缀的真实路径能转换为虚拟路径，不再触发 ValueError。

        输入本身带 ``\\\\?\\`` 前缀时，真实 ``resolve()`` 会保留前缀
        （``had_prefix=True``），无需打桩即可覆盖该分支。
        """
        root = tmp_path / "workspace"
        root.mkdir()
        target = root / "sub" / "file.txt"
        target.parent.mkdir()
        target.write_text("x", encoding="utf-8")
        backend = _SafeBackend(root_dir=str(root), virtual_mode=True)

        virtual = backend._to_virtual_path(Path("\\\\?\\" + str(target)))

        assert virtual == "/sub/file.txt"

    def test_official_to_virtual_path_fails_on_prefix(self, tmp_path: Path) -> None:
        """对照实验：官方实现遇到前缀路径会抛 ValueError。"""
        root = tmp_path / "workspace"
        root.mkdir()
        target = root / "sub" / "file.txt"
        target.parent.mkdir()
        target.write_text("x", encoding="utf-8")
        backend = FilesystemBackend(root_dir=str(root), virtual_mode=True)

        with pytest.raises(ValueError):
            backend._to_virtual_path(Path("\\\\?\\" + str(target)))


class TestBackendWiring:
    def test_mixin_mro_priority(self) -> None:
        """Mixin 必须位于 MRO 首位，保证覆盖生效。"""
        from agent.backends import (
            _ExtendedPathSafeFilesystemBackend,
            _ExtendedPathSafeLocalShellBackend,
        )
        from agent.sandbox_backend import SandboxedFilesystemBackend

        for cls in (
            _ExtendedPathSafeFilesystemBackend,
            _ExtendedPathSafeLocalShellBackend,
            SandboxedFilesystemBackend,
        ):
            # Mixin 必须排在 FilesystemBackend 之前，覆盖才会生效
            mro = cls.__mro__
            assert mro.index(ExtendedPathSafeBackendMixin) < mro.index(FilesystemBackend)
            # 中间件依赖的 isinstance 语义不受影响
            assert issubclass(cls, FilesystemBackend)
