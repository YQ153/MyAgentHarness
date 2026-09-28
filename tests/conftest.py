"""共享测试夹具。

设计原则：
- 配置与开发者本机 ``.env`` 完全隔离（``_env_file=None``），测试结果不受
  本机环境变量与密钥影响；
- SQLite 一律落在 ``tmp_path``，测试结束由 pytest 自动清理；
- 图层用假对象替代真实 LangGraph 图，测试不依赖任何 API Key。
"""

from __future__ import annotations

# WHY 提前显式导入这个子模块：本机 pydantic 版本把 ``pydantic.root_model`` 做成
# 惰性加载（``import pydantic`` 之后它并不在 ``sys.modules`` 里），而 ``mcp.types``
# 在**导入期**就执行 ``class JSONRPCMessage(RootModel[...])``，其内部要按模块名取
# ``sys.modules['pydantic.root_model']`` —— 没加载就抛 KeyError。
# 谁先被导入决定了测试能否收集：换个 --cov 参数就可能让整套用例在收集阶段崩掉。
# 在 conftest 里钉住这一句，使收集顺序不再影响结果（实测：缺了它，12 个测试文件
# 在「带多个 --cov 源」时收集失败；补上后全绿）。
import pydantic.root_model  # noqa: F401  （仅为副作用导入）

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from application.audit_context import bind_request_context, reset_request_context
from config import AppConfig, SessionRoot
from runtime.audit_store import AuditStore, open_audit_store
from runtime.thread_store import ThreadMetaStore, open_thread_store
from runtime.usage_store import UsageStore, open_usage_store


def make_config(tmp_path: Path, **overrides: Any) -> AppConfig:
    """构造与开发者本机环境隔离的测试配置。

    WHY ``_env_file`` 指向不存在的文件而不是 ``None``：pydantic-settings
    中 ``None`` 的语义是「不覆盖 model_config 里的 env_file」，.env 仍会被
    加载——本机 ``.env`` 的取值会静默改变测试的装配结果。

    Args:
        tmp_path: pytest 提供的临时目录。
        **overrides: 需要覆盖的字段（如 ``execution_mode``、``sessions_root``）。

    Returns:
        路径全部指向 ``tmp_path`` 的 ``AppConfig``。
    """
    params: dict[str, Any] = {
        "_env_file": tmp_path / "does-not-exist.env",
        "db_path": tmp_path / "agent.db",
        # WHY 不显式给 sessions_root：它默认派生自 ``db_path`` 的父目录，于是测试里
        # 每个用例的会话专属目录都落在自己的 ``tmp_path`` 下，天然隔离。
    }
    params.update(overrides)
    return AppConfig(**params)


def make_root(config: AppConfig, name: str = "workspace") -> SessionRoot:
    """构造一个测试用的会话根：``<数据目录>/<name>``。

    WHY 默认落在数据目录（``db_path`` 的父目录，即用例的 ``tmp_path``）下：绝大多数
    服务用例只关心「给我一个根」，而不关心它是用户选的工作空间还是应用建的专属目录
    ——两者在 ``SessionRoot`` 看来完全一样。默认名沿用 ``workspace``，是为了让「用例
    往根里写夹具文件」与「服务读那个根」两处用的是同一个目录（历史用例写的就是它）。

    Args:
        config: 测试配置。
        name: 子目录名；需要两个不同根的用例传不同的名字即可。

    Returns:
        指向 ``<数据目录>/<name>`` 的根，**目录已建好**。

    Note:
        WHY 这里就把目录建出来（生产侧是 ``ensure_directories`` 按需创建）：agent 层的
        backend 会断言「根必须是一个已存在的目录」（那是它做路径校验的前提）。夹具不建，
        每一条用例都会红在那条与它意图无关的断言上。
    """
    root = SessionRoot(config, config.db_path.parent / name)
    root.root.mkdir(parents=True, exist_ok=True)
    return root


def make_workspace_root(config: AppConfig, path: Path) -> SessionRoot:
    """构造一个指向**指定目录**的会话根（目录会被建出来）。

    WHY 与 ``make_root`` 并存：需要「两个不同根」或「根在某个特定位置」的用例用它，
    避免它们为了构造第二个根而不得不去读数据目录的内部布局。
    """
    path.mkdir(parents=True, exist_ok=True)
    return SessionRoot(config, path)


@pytest.fixture
def test_config(tmp_path: Path) -> AppConfig:
    """默认档位的隔离配置（执行档位为 disabled）。"""
    return make_config(tmp_path)


class StubSessionRegistry:
    """服务层与接口层用例用的最小工作区注册表替身。

    WHY 不用真的 ``SessionRegistry``：真装配会创建目录布局、重建技能物化视图、
    打开知识库连接——对一条只想验证「运行登记写没写库」的用例来说，那等于把单元测试
    变成集成测试，而且失败原因会混进与用例意图无关的磁盘与扩展依赖。
    真实注册表的解析与装配另有专门用例覆盖（``tests/application/test_session_registry.py``）。

    WHY 仍然实现 ``resolve`` 的完整语义：把它退化成「永远返回同一个根」，会让服务层那些
    「按会话取根」的用例失去意义——它们要验的正是「同一进程里两条会话拿到各自的根」。

    WHY 用 ``services_for`` 加一组可注入的服务：接口层用例各自只需要其中一个服务
    （文件面板 / 附件 / 技能 / 知识库），让替身把它们原样交出来，用例就不必为此拉起
    另外三个真服务。未注入的那几个保持 ``None``——若某条用例真的取用了它，会在
    ``AttributeError`` 上当场暴露，而不是悄悄换成一个空实现。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        files: object | None = None,
        attachments: object | None = None,
        skills: object | None = None,
        knowledge: object | None = None,
    ) -> None:
        """构造替身。

        Raises:
            ValueError: ``config`` 为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        self._config = config
        self._files = files
        self._attachments = attachments
        self._skills = skills
        self._knowledge = knowledge

    def managed_root(self, thread_id: str, *, preset: str = "") -> SessionRoot:
        """某条会话的专属根（与服务端同口径）。"""
        return SessionRoot(self._config, self._config.session_dir(thread_id), preset)

    def user_root(self, value: str | Path, *, preset: str = "") -> SessionRoot:
        """用户工作空间根（与服务端同口径：要求目录已存在）。"""
        from application.session_registry import resolve_user_path

        return SessionRoot(self._config, resolve_user_path(value), preset)

    def from_stored(self, stored: str, *, preset: str = "") -> SessionRoot:
        """库里的根还原（与服务端同口径）。"""
        return SessionRoot(self._config, Path(stored), preset)

    def pick_folder(self, initial: str | None = None) -> Any:
        """系统文件夹选择弹窗（与服务端同口径，直接复用同一个函数）。

        Note:
            这里**会真的弹窗**——用例必须把 ``application.session_registry.pick_folder``
            换成替身，否则 CI 上会多出一个没人关的窗口。
        """
        from application.session_registry import choose_workspace_folder

        return choose_workspace_folder(self._config, initial)

    def list_directories(self, path: str | None = None) -> Any:
        """目录列举（与服务端同口径，直接复用同一个函数）。"""
        from application.session_registry import list_directories

        return list_directories(path)

    async def resolve(
        self,
        *,
        requested: str | None = None,
        thread_id: str | None = None,
        record: dict | None = None,
        allow_missing: bool = False,
        preset: str | None = None,
    ) -> SessionRoot:
        """只做解析、不装配任何服务（语义与服务端逐条对齐）。"""
        from application.errors import SessionPresetLockedError, SessionRootNotReadyError

        has_value = requested is not None and str(requested).strip() != ""
        wanted_preset = (preset or "").strip()
        if thread_id is None:
            if not has_value:
                raise SessionRootNotReadyError("替身：没有会话也没有工作空间")
            return self.user_root(str(requested), preset=wanted_preset)

        current = record
        if current is None:
            current = {}
        stored = str(current.get("workspace") or "")
        stored_preset = str(current.get("preset") or "")
        if stored:
            locked = self.from_stored(stored, preset=stored_preset or wanted_preset)
            if has_value and str(self.user_root(str(requested)).root) != str(locked.root):
                from application.errors import SessionRootLockedError

                raise SessionRootLockedError(thread_id, str(locked.root), str(requested))
            # 与服务端同口径：库里已锁定的场景优先；给出不同取值即冲突。
            if stored_preset and wanted_preset and stored_preset != wanted_preset:
                raise SessionPresetLockedError(
                    f"会话 {thread_id}", stored_preset, wanted_preset
                )
            return locked
        if has_value:
            return self.user_root(str(requested), preset=wanted_preset)
        if allow_missing and not current:
            return self.managed_root(thread_id, preset=wanted_preset)
        return self.managed_root(thread_id, preset=wanted_preset)

    async def describe(
        self,
        *,
        thread_id: str | None = None,
        requested: str | None = None,
        allow_missing: bool = True,
        preset: str | None = None,
    ) -> Any:
        """根信息（与服务端同口径的最小实现）。"""
        from application.dto import WorkspaceInfo

        root = await self.resolve(
            requested=requested, thread_id=thread_id, allow_missing=allow_missing, preset=preset
        )
        has_value = requested is not None and str(requested).strip() != ""
        if not root.root.is_dir():
            raise RuntimeError(f"会话根目录已不存在：{root.root}")
        return WorkspaceInfo(
            path=str(root.root),
            name=root.root.name or str(root.root),
            bound=has_value,
            locked=False,
        )

    async def services_for(
        self,
        *,
        requested: str | None = None,
        thread_id: str | None = None,
        record: dict | None = None,
        allow_missing: bool = False,
        preset: str | None = None,
    ) -> Any:
        """把注入的那几个服务按会话根原样交出来。"""
        from application.session_registry import SessionServices

        root = await self.resolve(
            requested=requested,
            thread_id=thread_id,
            record=record,
            allow_missing=allow_missing,
            preset=preset,
        )
        return SessionServices(
            root=root,
            files=self._files,  # type: ignore[arg-type]
            attachments=self._attachments,  # type: ignore[arg-type]
            skills=self._skills,  # type: ignore[arg-type]
            knowledge=self._knowledge,  # type: ignore[arg-type]
        )

    async def services(self, root: SessionRoot) -> Any:
        """不装配任何服务：替身没有技能视图可对齐，按契约接住这次调用即可。

        WHY 需要这个空实现、而不是继续抛 ``AssertionError``：运行链路在取图之前会调一次
        ``services()``，把技能视图对齐到本次的（工作空间 + 场景）——正式实现里那是「预设
        技能到底进不进上下文」的落点。替身没有视图，这次调用的效果就是「什么都不用做」；
        继续抛错只会让所有用替身的运行用例失败，而失败原因与它们要验的东西无关。
        """
        del root
        return None


@pytest.fixture
async def thread_store(tmp_path: Path) -> AsyncIterator[ThreadMetaStore]:
    """落在临时目录里的会话元数据存储。"""
    async with open_thread_store(tmp_path / "threads.db") as store:
        yield store


@pytest.fixture
async def audit_store(tmp_path: Path) -> AsyncIterator[AuditStore]:
    """落在临时目录里的审计日志存储。"""
    async with open_audit_store(tmp_path / "audit.db") as store:
        yield store


@pytest.fixture
async def usage_store(tmp_path: Path) -> AsyncIterator[UsageStore]:
    """落在临时目录里的 token 用量存储。"""
    async with open_usage_store(tmp_path / "usage.db") as store:
        yield store


_HOST_ENV_LEAKS = (
    # 列表型：为试跑联网工具而导出过，会改变扩展工具的装配结果
    "CUSTOM_TOOL_MODULES",
    "MCP_SERVERS",
    # 嵌入后端：任何一项被导出都会让「默认档位不装配后端」的断言失败，而失败形态
    # 看起来像代码坏了，实际只是本机环境不同——与上面两个变量同一类问题
    "EMBEDDING_BACKEND",
    "EMBEDDING_MODEL",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_DIMS",
    "EMBEDDING_PYTHON",
)
"""会被 shell 导出、且会改变装配结果的环境变量。"""


@pytest.fixture(autouse=True)
def _isolated_extension_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉本机可能导出的扩展工具变量。

    WHY 必须清：``make_config`` 的 ``_env_file`` 只挡 ``.env``，**挡不住真实环境
    变量**。一旦 ``CUSTOM_TOOL_MODULES`` 被导出（例如为了试跑联网工具而
    ``CUSTOM_TOOL_MODULES=web_tools`` 跑一条命令，或直接写进 shell 配置），
    所有断言「没有扩展工具」的用例都会成片假失败——那种失败看起来像代码坏了，
    实际只是本机环境不同。

    WHY 用夹具而不是在 ``make_config`` 里给这些字段钉默认值：pydantic-settings 中
    init 参数的优先级**高于**环境变量，钉死会让「验证环境变量解析」的那组用例
    永远读不到自己设的值。删变量则两边都成立：用例内 ``setenv`` 照样生效，
    其余用例拿到干净环境。
    """
    for name in _HOST_ENV_LEAKS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolated_request_context() -> Iterator[None]:
    """每个用例前后清理审计请求上下文。

    WHY 自动生效：``contextvars`` 的默认值是进程级的，某个用例若忘记回滚，
    泄漏的 IP/UA 会串到后续用例的审计断言上——这类串扰只在批量跑测试时
    出现，且表现为随机失败。
    """
    token = bind_request_context(ip="", user_agent="")
    try:
        yield
    finally:
        reset_request_context(token)
