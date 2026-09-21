"""各存储 ``open_*`` 上下文管理器的异常归因契约。

WHY 单独成文件：这条契约由 6 个 ``open_*`` **共同**承担——会话元数据 / 审计日志 /
用量记录 / 技能状态 / 知识库 / 长期记忆。它们各自的业务测试都在自己文件里，
而约定管的是**同一件事**，逐文件各写一条只能证明写了六遍。

WHY 值得单独守：``yield`` 若落在捕获初始化异常的 ``try`` 里，``async with`` 主体
（调用方的装配或业务代码）抛出的异常也会被接住，记成「<某表> 初始化失败」并附上堆栈。
实测代价：一次装配失败时，真正的原因只有一句话，日志里却是 5 条「XX 初始化失败」加
5 份重复堆栈——排查方向被引向数据库，而数据库根本没问题。

两件事各有一条断言：主体异常必须原样传播、不得被误记为初始化失败；真·初始化失败
（连不上库）必须仍留下「初始化失败」记录——修归因不等于把真故障一并静音。

WHY 在实现已经收敛之后仍然保留本文件：这个结构现在只剩一份实现
（``runtime/sqlite_lifecycle.py``），本文件因此从"六份副本的共同约定"变成了
**那份实现的行为规格**——它把最容易在重构中被顺手改坏的那条性质，从注释里的一句话
变成了会失败的断言。6 个入口逐条参数化也不是形式：入口的差异（知识库要加载向量扩展、
长期记忆要自动提交）恰恰是最可能有人"绕开公共实现"的地方，而绕开之后就没人守它了。
"""

from __future__ import annotations

import logging
import sqlite3
from functools import partial
from pathlib import Path
from typing import Any, Callable

import pytest

from runtime.audit_store import open_audit_store
from runtime.knowledge_store import open_knowledge_store
from runtime.skill_store import open_skill_store
from runtime.store import open_store
from runtime.thread_store import open_thread_store
from runtime.usage_store import open_usage_store

_OpenStore = Callable[[Path], Any]

_OPENERS: list[tuple[str, _OpenStore]] = [
    ("会话元数据", open_thread_store),
    ("审计日志", open_audit_store),
    ("用量记录", open_usage_store),
    ("技能状态", open_skill_store),
    # 向量关闭：本文件验的是异常归因，把 sqlite-vec 能否加载混进来只会引入一条与本约定
    # 无关的环境依赖（扩展没装好的机器上会红，而结论与它无关）。
    ("知识库", partial(open_knowledge_store, dims=512, vector_enabled=False)),
    # 长期记忆：此前不在本表里，而它恰好是**唯一**不满足第二条断言的实现——连接与
    # PRAGMA 在任何 except 之外，真·初始化失败时反而一条记录都没有。契约的覆盖边界
    # 与"唯一不达标的那个"重合，等于没覆盖；补上之后这类重合不会再悄悄发生。
    ("长期记忆", open_store),
]

# 用一个具名变量承载装饰器：多个实现各写一遍 parametrize 样板会让文件被样板占满，
# 而真正要读的只有下面两条断言。
_OPENER_CASES = pytest.mark.parametrize(
    ("label", "open_store"), _OPENERS, ids=[label for label, _ in _OPENERS]
)


@_OPENER_CASES
async def test_body_exception_is_never_blamed_on_initialization(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    label: str,
    open_store: _OpenStore,
) -> None:
    """主体抛错时：异常原样传出，且不产生该存储的「初始化失败」记录。"""
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError, match="主体炸了"):
            async with open_store(tmp_path / "meta.db"):
                raise RuntimeError("主体炸了")

    assert "初始化失败" not in caplog.text, f"{label}：主体异常被误记成初始化失败"
    # 顺带守住「正常就绪」的日志不被误发：走到 yield 之前就抛了，不该说「已就绪」
    assert "已就绪" in caplog.text, f"{label}：连初始化完成都未记录，用例前提不成立"


@_OPENER_CASES
async def test_real_initialization_failure_is_still_reported(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    label: str,
    open_store: _OpenStore,
) -> None:
    """连不上库（把库路径占成目录）：仍要留下「初始化失败」。

    WHY 补这一条：把 ``yield`` 移出捕获范围时，最容易顺手把连接建立也挪出去，
    于是真·初始化失败变得悄无声息——那比误归因更难查。
    """
    bad_path = tmp_path / "meta.db"
    bad_path.mkdir()
    caplog.clear()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(sqlite3.OperationalError):
            async with open_store(bad_path):
                pytest.fail(f"{label}：库路径不可用时不该走到主体")

    assert "初始化失败" in caplog.text, f"{label}：真初始化失败没有留下记录"
