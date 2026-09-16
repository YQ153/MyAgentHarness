"""共享测试夹具。

设计原则：
- 配置与开发者本机 ``.env`` 完全隔离（``_env_file=None``），测试结果不受
  本机环境变量与密钥影响；
- SQLite 一律落在 ``tmp_path``，测试结束由 pytest 自动清理；
- 图层用假对象替代真实 LangGraph 图，测试不依赖任何 API Key。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from application.audit_context import bind_request_context, reset_request_context
from config import AppConfig
from runtime.audit_store import AuditStore, open_audit_store
from runtime.thread_store import ThreadMetaStore, open_thread_store


def make_config(tmp_path: Path, **overrides: Any) -> AppConfig:
    """构造与开发者本机环境隔离的测试配置。

    WHY ``_env_file`` 指向不存在的文件而不是 ``None``：pydantic-settings
    中 ``None`` 的语义是「不覆盖 model_config 里的 env_file」，.env 仍会被
    加载——本机 ``AUTH_MODE=oidc`` 会静默改变测试的权限语义。

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
