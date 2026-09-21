"""宿主系统文件夹选择弹窗的调用与失败分类。

这一层最容易出错的不是「能不能弹出窗口」，而是**弹不出来的时候说什么**：

1. **环境不支持**（容器 / 无桌面 / 缺 tkinter）——必须说清原因，因为这个部署形态下唯一
   的出路是换一条路径（网页内浏览），而不是重试；
2. **超时**——必须与「用户取消」分开：说成取消会让用户在以为已经关掉的窗口里继续点；
3. **一次只弹一个**——两个标签页各点一次会叠出两个一模一样的对话框，用户关掉第一个
   之后仍被第二个挡住，而他不知道自己开了两个。

所有用例都注入 runner 替身：**绝不能在测试里真的弹窗**，否则 CI 机器上会多出一个
没人关的窗口，并且测试会挂到超时。
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from runtime import folder_picker
from runtime.folder_picker import (
    FolderPickerBusyError,
    FolderPickerTimeoutError,
    FolderPickerUnavailableError,
)


#: 夹具会替换掉的真实实现；验「环境不支持」的两条用例需要把它们换回来。
_REAL_DISPLAY_HINT = folder_picker._display_hint
_REAL_TKINTER_AVAILABLE = folder_picker._tkinter_available


class _Runner:
    """子进程替身：记录 argv，返回预置结果。"""

    def __init__(
        self,
        *,
        stdout: str = '{"path": ""}',
        stderr: str = "",
        returncode: int = 0,
        raises: Exception | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.raises = raises
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> folder_picker._Completed:
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        return folder_picker._Completed(
            returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


@pytest.fixture(autouse=True)
def _pretend_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    """假装这台机器有图形环境与 tkinter。

    WHY 全局夹具：本文件验的是「拿到选择之后怎么处理」，而「有没有桌面」由两条独立用例
    专门覆盖。不假装的话，本文件在无桌面的 CI 上会整体走成「环境不支持」——测试全绿
    但实际上什么都没验。
    """
    monkeypatch.setattr(folder_picker, "_display_hint", lambda: None)
    monkeypatch.setattr(folder_picker, "_tkinter_available", lambda: True)


def test_returns_the_selected_path(tmp_path: Path) -> None:
    """正常路径：子进程输出里的路径被解析成绝对路径。"""
    chosen = tmp_path / "my-app"
    chosen.mkdir()
    runner = _Runner(stdout=f'{{"path": "{chosen.as_posix()}"}}\n')

    result = folder_picker.pick_folder(initial_dir=tmp_path, runner=runner)

    assert result == chosen.resolve()
    assert runner.calls, "应当真的去调用子进程"


def test_cancel_returns_none(tmp_path: Path) -> None:
    """用户取消 → ``None``，而不是异常。

    WHY 单独钉：把它当成失败会让界面弹一条红条，而用户只是改了主意——那条红条还会
    掩盖真正的失败（例如环境不支持）。
    """
    runner = _Runner(stdout='{"path": ""}\n')

    assert folder_picker.pick_folder(initial_dir=tmp_path, runner=runner) is None


def test_unparsable_output_is_treated_as_cancel() -> None:
    """子进程多打印了别的东西时不把半截路径当选择。

    WHY：tkinter 与 Tcl 会往 stdout 打告警。按「最后一行是 JSON」解析，坏数据退化成
    取消，好过把一个残缺字符串当作用户选中的目录。
    """
    runner = _Runner(stdout='some tcl warning\n{"path": ""}\n')

    assert folder_picker.pick_folder(runner=runner) is None


def test_initial_dir_is_passed_to_the_child(tmp_path: Path) -> None:
    """起始目录会被传给子进程（对话框打开时落在用户当前的选择上）。"""
    runner = _Runner()

    folder_picker.pick_folder(initial_dir=tmp_path, runner=runner)

    assert str(tmp_path) in runner.calls[0]


def test_missing_initial_dir_is_not_passed(tmp_path: Path) -> None:
    """起始目录不存在时传空串，让 tkinter 自己找位置。

    WHY：把一个不存在的路径交给 ``askdirectory(initialdir=...)`` 会让部分平台弹窗直接
    失败或落在一个莫名其妙的位置——而用户根本不知道发生了什么。
    """
    runner = _Runner()

    folder_picker.pick_folder(initial_dir=tmp_path / "gone", runner=runner)

    assert runner.calls[0][-1] == ""


def test_non_zero_exit_reports_the_reason(tmp_path: Path) -> None:
    """子进程失败时把 stderr 的最后一行带出来。

    WHY：缺 tkinter、无显示器的真实原因都只出现在子进程的 stderr 里；不带上它，用户
    只会看到一句「启动失败」，而修法（装 python3-tk / 换网页内浏览）无从谈起。
    """
    runner = _Runner(
        returncode=1,
        stderr="Traceback (most recent call last):\nModuleNotFoundError: No module named 'tkinter'\n",
    )

    with pytest.raises(FolderPickerUnavailableError, match="tkinter"):
        folder_picker.pick_folder(runner=runner)


def test_timeout_is_not_reported_as_a_cancel(tmp_path: Path) -> None:
    """超时报「超时」，不说成取消。

    WHY：说成取消会让用户在以为已经关掉的窗口里继续点选，而服务端已经不再理会它了。
    """
    runner = _Runner(raises=subprocess.TimeoutExpired(cmd="python", timeout=1.0))

    with pytest.raises(FolderPickerTimeoutError):
        folder_picker.pick_folder(runner=runner)


def test_second_dialog_is_rejected_while_the_first_waits(tmp_path: Path) -> None:
    """已有弹窗在等待时，第二次请求直接 409（对应 ``FolderPickerBusyError``）。

    WHY：两个标签页各点一次会在服务端屏幕上叠出两个一模一样的对话框，用户关掉第一个
    之后仍被第二个挡住——而他不知道自己开了两个。
    """
    acquired = folder_picker._ONE_AT_A_TIME.acquire(blocking=False)
    assert acquired, "用例前置条件：锁应当是空闲的"
    try:
        with pytest.raises(FolderPickerBusyError):
            folder_picker.pick_folder(runner=_Runner())
    finally:
        folder_picker._ONE_AT_A_TIME.release()


def test_lock_is_released_after_a_failure(tmp_path: Path) -> None:
    """失败之后锁必须释放，否则后续请求会永远撞「已有弹窗在等待」。

    WHY 单独钉：这把锁是模块级状态，泄漏一次就会让整个功能在本次进程剩余时间里失效——
    而症状（每次都 409）与「另一个弹窗没关」一模一样。
    """
    runner = _Runner(returncode=1, stderr="boom")

    with pytest.raises(FolderPickerUnavailableError):
        folder_picker.pick_folder(runner=runner)

    assert folder_picker._ONE_AT_A_TIME.acquire(blocking=False) is True
    folder_picker._ONE_AT_A_TIME.release()


def test_missing_display_fails_before_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没有图形环境时**先报错、不启动子进程**。

    WHY 必须在 spawn 之前判定：在没有 ``DISPLAY`` 的服务器上启动 tkinter，会得到一个
    挂在后台等待的进程——比直接报错更糟（用户既看不到窗口，也得不到原因）。

    WHY 要把真实实现换回来：``_pretend_desktop`` 夹具把它替换成了「有桌面」，
    而这条用例验的正是那条分支本身。
    """
    monkeypatch.setattr(folder_picker, "_display_hint", _REAL_DISPLAY_HINT)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setenv("DISPLAY", "")
    monkeypatch.setenv("WAYLAND_DISPLAY", "")
    runner = _Runner()

    with pytest.raises(FolderPickerUnavailableError, match="图形环境"):
        folder_picker.pick_folder(runner=runner)

    assert runner.calls == []


def test_missing_tkinter_fails_before_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 tkinter 时同样先报错，并把「怎么装」写进消息里。

    WHY 不注入 runner：探测只在「用默认执行方式」时进行（注入替身意味着调用方自己负责
    环境），因此这里必须走默认分支才能触到那个判断。为防止真跑子进程，把默认执行方式
    换成一个会在被调用时让用例失败的探针。
    """

    def exploding_default(argv: Sequence[str], timeout: float) -> folder_picker._Completed:
        raise AssertionError(f"环境不支持时不应启动子进程：{argv}")

    monkeypatch.setattr(folder_picker, "_tkinter_available", lambda: False)
    monkeypatch.setattr(folder_picker, "_default_runner", exploding_default)

    with pytest.raises(FolderPickerUnavailableError, match="tkinter"):
        folder_picker.pick_folder()


def test_real_runner_is_used_when_none_is_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """不注入 runner 时走真实子进程实现（``_default_runner``）。

    WHY 用替换过的 ``_default_runner`` 断而不是真跑：这里要验的是「默认分支接对了没有」，
    真跑会弹窗。替换之后既钉住接线，又不会在 CI 上留下窗口。
    """
    used: list[Sequence[str]] = []

    def fake_default(argv: Sequence[str], timeout: float) -> folder_picker._Completed:
        used.append(argv)
        return folder_picker._Completed(returncode=0, stdout='{"path": ""}')

    monkeypatch.setattr(folder_picker, "_default_runner", fake_default)

    result = folder_picker.pick_folder()

    assert result is None
    assert used, "默认应当走 _default_runner"


def test_a_stuck_dialog_can_be_recovered_by_a_later_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上一次超时之后，下一次仍能正常工作。

    WHY：超时是「用户在屏幕上留了个窗口」，不是「功能坏了」。锁没释放的话，那个被遗留的
    窗口会让整个功能在本进程内永久失效。
    """
    failing = _Runner(raises=subprocess.TimeoutExpired(cmd="python", timeout=1.0))
    with pytest.raises(FolderPickerTimeoutError):
        folder_picker.pick_folder(runner=failing)

    ok = _Runner(stdout='{"path": ""}')
    assert folder_picker.pick_folder(runner=ok) is None
    assert threading.active_count() >= 1
