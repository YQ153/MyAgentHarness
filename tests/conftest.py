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
from config import AppConfig
from runtime.audit_store import AuditStore, open_audit_store
from runtime.thread_store import ThreadMetaStore, open_thread_store
from runtime.usage_store import UsageStore, open_usage_store


def make_config(tmp_path: Path, **overrides: Any) -> AppConfig:
    """构造与开发者本机环境隔离的测试配置。

    WHY ``_env_file`` 指向不存在的文件而不是 ``None``：pydantic-settings
    中 ``None`` 的语义是「不覆盖 model_config 里的 env_file」，.env 仍会被
    加载——本机 ``AUTH_MODE=apikey`` 会静默改变测试的权限语义。

    Args:
        tmp_path: pytest 提供的临时目录。
        **overrides: 需要覆盖的字段（如 ``auth_mode``）。

    Returns:
        路径全部指向 ``tmp_path``、认证默认关闭的 ``AppConfig``。
    """
    params: dict[str, Any] = {
        "_env_file": tmp_path / "does-not-exist.env",
        # 显式默认值抵御真实环境变量的泄漏（如 shell 里导出过 AUTH_MODE）
        "auth_mode": "disabled",
        "workspace": tmp_path / "workspace",
        "memory_file": tmp_path / "workspace" / "AGENTS.md",
        "db_path": tmp_path / "agent.db",
        "skill_dirs": [tmp_path / "workspace" / "skills"],
    }
    params.update(overrides)
    return AppConfig(**params)


@pytest.fixture
def test_config(tmp_path: Path) -> AppConfig:
    """默认档位的隔离配置（disabled 认证、disabled 执行）。"""
    return make_config(tmp_path)


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


_HOST_ENV_LEAKS = ("CUSTOM_TOOL_MODULES", "MCP_SERVERS")
"""会被 shell 导出、且会改变装配结果的列表型变量。"""


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
