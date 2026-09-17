"""执行登记处的回归测试：作用域绑定、登记/反登记、按作用域中止。

WHY 单独成文件：登记处是「停止运行」能真正终止进程树的唯一通道，而它的
难点全在边界——未绑定作用域时不能登记、同一作用域要能容纳多条命令、
单个句柄抛错不能连累其他句柄。这些边界与任何业务功能都无关。
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from runtime import execution_registry as registry_module
from runtime.execution_registry import (
    abort_scope,
    bound_scope,
    current_scope,
    register,
    unregister,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    """每个用例结束后清空登记处。

    WHY 直接清私有字典：登记处是进程级单例，而用例为了方便常常只登记不
    反登记（真实调用方在 ``finally`` 里做）。不清会在用例之间串味，前一个
    用例遗留的句柄会让后一个用例的计数断言莫名其妙地变多。
    """
    yield
    with registry_module._guard:  # noqa: SLF001 - 测试专用清理
        registry_module._registry.clear()  # noqa: SLF001


class FakeHandle:
    """记录自己是否被中止的执行句柄替身；可配置为 abort 时抛错。"""

    def __init__(self, *, raises: bool = False) -> None:
        self.aborted = 0
        self.raises = raises

    def abort(self) -> None:
        self.aborted += 1
        if self.raises:
            raise RuntimeError("句柄已失效")


# ------------------------------------------------------------------ 作用域


def test_current_scope_is_none_by_default():
    assert current_scope() is None


def test_bound_scope_sets_and_restores():
    with bound_scope("t1"):
        assert current_scope() == "t1"
    assert current_scope() is None


def test_bound_scope_restores_on_exception():
    with pytest.raises(RuntimeError):
        with bound_scope("t1"):
            raise RuntimeError("boom")
    assert current_scope() is None


def test_nested_scope_restores_outer():
    with bound_scope("outer"):
        with bound_scope("inner"):
            assert current_scope() == "inner"
        assert current_scope() == "outer"


def test_unbound_scope_is_ignored():
    handle = FakeHandle()
    with bound_scope(None):
        assert register(handle) is False
    # 未绑定时反登记也必须是 no-op：调用方按「登记成功才反登记」写代码，
    # 但 CLI 等场景可能无条件调用。
    unregister(handle)
    assert abort_scope("t1") == 0


# ------------------------------------------------------------------ 登记与中止


def test_abort_scope_notifies_registered_handles():
    first = FakeHandle()
    second = FakeHandle()
    with bound_scope("t1"):
        assert register(first) is True
        assert register(second) is True
        # WHY 同作用域可容纳多条命令：并行工具会同时跑多个命令，
        # 只留一个句柄会让「停止」漏掉其余进程树。
        assert abort_scope("t1") == 2
    assert first.aborted == 1
    assert second.aborted == 1


def test_abort_scope_is_scoped():
    mine = FakeHandle()
    other = FakeHandle()
    with bound_scope("t1"):
        register(mine)
    with bound_scope("t2"):
        register(other)

    assert abort_scope("t1") == 1
    assert mine.aborted == 1
    assert other.aborted == 0


def test_unregister_removes_handle():
    handle = FakeHandle()
    with bound_scope("t1"):
        register(handle)
        unregister(handle)
        assert abort_scope("t1") == 0
    assert handle.aborted == 0


def test_register_is_idempotent():
    """WHY 同一句柄重复登记不应被中止两次：终止是一次性动作，
    重复执行只会让日志与指标失真。"""
    handle = FakeHandle()
    with bound_scope("t1"):
        register(handle)
        register(handle)
        assert abort_scope("t1") == 1
    assert handle.aborted == 1


def test_abort_scope_survives_failing_handle():
    broken = FakeHandle(raises=True)
    healthy = FakeHandle()
    with bound_scope("t1"):
        register(broken)
        register(healthy)
        assert abort_scope("t1") == 1

    # WHY 关键断言：一个句柄抛错（进程已退出、句柄已关闭）不能让同会话的
    # 其他进程树逃过终止——那正是「停止了却还在跑」的成因。
    assert healthy.aborted == 1
    assert broken.aborted == 1


def test_registry_is_reusable_after_scope_gone():
    """WHY 覆盖回收：作用域用完不清理，登记处会随会话数单调增长，
    长驻进程最终会被自己的字典拖住。"""
    handle = FakeHandle()
    with bound_scope("t1"):
        register(handle)
        unregister(handle)
    assert abort_scope("t1") == 0

    with bound_scope("t1"):
        another = FakeHandle()
        register(another)
        assert abort_scope("t1") == 1
        unregister(another)


# ------------------------------------------------------------------ 并发


def test_registry_is_thread_safe():
    """WHY 覆盖并发：登记发生在工作线程、中止发生在事件循环线程，
    二者没有任何同步关系，只能靠登记处自己的锁保证不出错。"""

    handles = [FakeHandle() for _ in range(50)]
    errors: list[BaseException] = []

    def _worker(items: list[FakeHandle]) -> None:
        try:
            with bound_scope("t1"):
                for item in items:
                    register(item)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(handles[i::5],)) for i in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors

    assert abort_scope("t1") == 50
    assert all(handle.aborted == 1 for handle in handles)


def test_abort_scope_accepts_any_scope_value() -> None:
    """未登记的作用域返回 0，而不是抛错——停止时图常常正在等模型，
    此时「没有在跑的命令」是正常现象。"""
    assert abort_scope("never-registered") == 0


def test_handles_must_implement_abort() -> None:
    """WHY 断言协议：登记处只在调用时才碰到 ``abort``，缺方法的对象
    会在最坏的时刻（用户按停止）才炸。"""

    class Broken:
        def __init__(self) -> None:
            self.state: Any = None

    handle: Any = Broken()
    with bound_scope("t1"):
        register(handle)
        assert abort_scope("t1") == 0
        unregister(handle)
