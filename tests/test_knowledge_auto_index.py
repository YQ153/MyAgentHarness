"""知识库自动索引的装配侧：装配即跑首轮、关闭即停任务、开关能关掉它。

与 ``tests/application/test_knowledge_sync.py`` 的分工：那边验「一轮同步算得对不对」，
这里验「它会不会被真的挂上、会不会被真的摘掉」——后台任务最典型的失效是**静默的**：
任务没起来不会报错，只是索引一直停在装配那一刻；任务没停会在退出时留一串
"Task was destroyed but it is pending"。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import knowledge_runtime
from application.knowledge_service import KnowledgeService
from config import AppConfig
from knowledge_runtime import close_service, ensure_service, peek_service
from tests.conftest import make_config, make_root

_DOC = "# 运维手册\n\n登录接口偶发超时，p99 达到 3 秒，先看连接池的 max_overflow。"

_POLL_SECONDS = 5.0
"""等待首轮落地的最长时间。

WHY 用轮询而不是直接断言：首轮跑在后台协程里，与用例并发。固定 ``sleep`` 会让用例
在快机器上浪费时间、在慢机器上偶发失败；轮询到「索引出现」为止才是稳定的判据。
"""


@pytest.fixture(autouse=True)
async def _reset_knowledge_runtime() -> AsyncIterator[None]:
    """每个用例前后清空进程级句柄。

    WHY 必须清：句柄是模块级状态，上一个用例装好的实例（连同它**正在跑的后台任务**）
    会留到下一个用例；那种串扰只在「连起来跑」时出现。
    """
    await knowledge_runtime.close_service()
    yield
    await knowledge_runtime.close_service()


async def _wait_for_documents(service: KnowledgeService, timeout: float = _POLL_SECONDS) -> list[str]:
    """轮询直到索引里出现文档，或超时返回空列表。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        documents = await service.list_documents()
        paths = [item["source_path"] for item in documents["items"]]
        if paths:
            return paths
        if loop.time() >= deadline:
            return []
        await asyncio.sleep(0.05)


def _write_doc(config: AppConfig, relative: str = "doc.md") -> None:
    target = make_root(config).root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_DOC, encoding="utf-8")


async def test_first_sync_runs_without_manual_trigger(tmp_path: Path) -> None:
    """装配后不做任何手动操作，工作区里的文档也应被索引。

    WHY 这条是整个功能的意义所在：在此之前索引只随「人工点面板 / Agent 调工具」刷新，
    文档改过之后检索到的仍是旧内容——而那个现象看起来只是「搜得不准」。
    """
    config = make_config(tmp_path)
    _write_doc(config)
    root = make_root(config).root

    service = await ensure_service(config, workspace=root)

    assert await _wait_for_documents(service) == ["/doc.md"]
    assert peek_service(root) is service


async def test_closing_service_stops_the_worker(tmp_path: Path) -> None:
    """关闭知识库后句柄必须清空——后台任务随退出栈一起被停止。

    WHY 断言句柄而不是任务对象：任务是否真的停了，外部可见的事实就是「句柄没了」；
    真正要防的是退出时那串 "Task was destroyed but it is pending"。
    """
    config = make_config(tmp_path)
    _write_doc(config)
    root = make_root(config).root

    service = await ensure_service(config, workspace=root)
    assert await _wait_for_documents(service) == ["/doc.md"]

    await close_service()

    assert peek_service(root) is None


async def test_disabled_switch_keeps_index_empty(tmp_path: Path) -> None:
    """``KNOWLEDGE_AUTO_INDEX=false`` 时不得自动索引（回到手动刷新的行为）。"""
    config = make_config(tmp_path, knowledge_auto_index=False)
    _write_doc(config)
    root = make_root(config).root

    service = await ensure_service(config, workspace=root)

    assert await _wait_for_documents(service, timeout=0.5) == []
    # 手动调用仍然可用：关掉的只是「自动」，不是能力本身
    summary = await service.index_workspace()
    assert summary["indexed"] == 1, summary


__all__ = [
    "test_closing_service_stops_the_worker",
    "test_disabled_switch_keeps_index_empty",
    "test_first_sync_runs_without_manual_trigger",
]
