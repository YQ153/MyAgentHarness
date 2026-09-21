"""调用宿主操作系统的文件夹选择弹窗。

WHY 必须由**服务端**弹窗、而不是浏览器：浏览器页面拿不到宿主的绝对路径——
``<input webkitdirectory>`` 只给出相对名，File System Access API 只给出一个 handle。
要拿到 ``D:\\projects\\my-app`` 这种取值，只能由服务端进程在自己的桌面上弹出原生对话框。

WHY 放在 ``runtime``：它会**启动一个外部进程**（Python 子进程跑 tkinter），属于宿主侧
进程控制，与 ``runtime/sandbox`` 同类。放在 ``application`` 里等于让应用层持有进程
管理细节；而 ``interfaces`` 按分层契约不能直接依赖 ``runtime``（必须经应用层）。

代价必须说清楚：这个对话框出现在**服务端那台机器**的屏幕上，而不是访问浏览器的人眼前。
因此它只适用于「服务端就跑在你自己机器上」这种形态；容器、无显示器的服务器、以及
服务端与浏览器不在同一台机器时都会失败——那时应当退回到网页内浏览（见
``/api/workspaces/browse``）。

WHY 用子进程而不是在工作线程里直接开 tkinter：

1. tkinter 对线程亲和性敏感，在工作线程里创建/销毁 Tk 根在部分平台会偶发崩溃；
2. 用户可能永远不关那个对话框，子进程可以被超时干净地杀掉，而进程内的线程做不到。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)


class FolderPickerUnavailableError(RuntimeError):
    """当前环境无法弹出系统文件夹选择弹窗（无桌面 / 缺 tkinter / 取不到解释器）。"""


class FolderPickerBusyError(RuntimeError):
    """已经有一个文件夹选择弹窗在等待操作。

    WHY 单独成一个类型：它对应 409（稍后重试即可），而「环境不支持」是 501（改环境才行）。
    混成一类会让调用方在唯一该重试的场景里放弃重试。
    """


class FolderPickerTimeoutError(RuntimeError):
    """弹窗在超时时间内没有被关闭。"""


DEFAULT_TITLE = "选择工作区目录"
"""对话框标题。会显示在系统弹窗的标题栏上，用于说明「这个选择在为什么服务」。"""

DEFAULT_TIMEOUT_SECONDS = 300.0
"""等待用户关闭对话框的上限（秒）。

WHY 给到 5 分钟：挑目录本来就慢，用户可能翻很久。超时不是为了防手慢，而是防止一个
被遗留在屏幕上的对话框永久占住那把锁，让后续请求全部撞 ``FolderPickerBusyError``。
"""

_TK_SNIPPET = """
import json
import sys

import tkinter
from tkinter import filedialog

title = sys.argv[1] or "选择目录"
initial = sys.argv[2] or None

root = tkinter.Tk()
root.withdraw()
# WHY 置顶：对话框在后台被别的窗口盖住时，调用方（浏览器那边）在等一个它看不见的窗口，
# 而用户以为按钮没反应。置顶失败不是致命问题（部分平台不支持该属性），忽略即可。
try:
    root.attributes("-topmost", True)
except Exception:
    pass
root.update()
try:
    chosen = filedialog.askdirectory(title=title, initialdir=initial, mustexist=True)
finally:
    root.destroy()
print(json.dumps({"path": chosen or ""}))
"""


@dataclass(frozen=True)
class _Completed:
    """子进程结果的最小形状（便于测试注入替身，不必构造真的 ``CompletedProcess``）。"""

    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str], float], _Completed]
"""执行子进程的方式：``(argv, timeout) -> _Completed``。测试注入替身，避免真弹窗。"""

_ONE_AT_A_TIME = threading.Lock()
"""全局互斥：同一时刻只允许一个系统弹窗。

WHY 需要它：两个浏览器标签页各点一次「选择文件夹」，就会在服务端的屏幕上叠出两个一模一样的
对话框，用户关掉第一个之后仍然被第二个挡住——而他并不知道自己开了两个。
"""


def _default_runner(argv: Sequence[str], timeout: float) -> _Completed:
    """用当前解释器跑子进程，并按需隐藏控制台窗口。

    WHY ``CREATE_NO_WINDOW``：Windows 上 ``sys.executable`` 是控制台程序，直接 spawn 会
    闪一个黑框。它只隐藏控制台，不影响 tkinter 的图形窗口。
    """
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(  # noqa: S603 参数是本模块构造的常量序列，无外部拼入
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=creationflags,
        check=False,
    )
    return _Completed(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


@lru_cache(maxsize=1)
def _tkinter_available() -> bool:
    """本机解释器里能不能 ``import tkinter``。

    WHY 缓存：这是一次子进程探测（百毫秒级），而「装没装 tkinter」在一个进程生命周期内
    不会变。每次点按钮都探一次，等于把一次固定开销摊到每次交互上。

    WHY 探测而不是直接 ``import tkinter``：本模块会在**没有图形环境**的部署里被导入；
    顶层导入 tkinter 会把「缺这个包」变成「整个应用起不来」。
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", "import tkinter"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("探测 tkinter 失败：%s", exc)
        return False
    return completed.returncode == 0


def _display_hint() -> str | None:
    """返回「没有图形环境」的说明；能弹窗时返回 ``None``。

    WHY 在 POSIX 上先看环境变量而不直接试：没有 ``DISPLAY`` 时 tkinter 会抛一句
    ``no display name and no $DISPLAY environment variable``——把它翻译成「这个进程
    没有桌面会话，因此无法弹出系统对话框」对用户才有意义。
    """
    if os.name == "nt":
        # Windows 上：服务以本地系统账户跑、或在没有交互式会话的环境里跑，弹窗都不会出现。
        # 这里无法静态判定，交给子进程失败后再解释（见 pick_folder 的错误分支）。
        return None
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "当前进程没有图形环境（DISPLAY / WAYLAND_DISPLAY 都未设置），无法弹出系统对话框"
    return None


def pick_folder(
    *,
    title: str = DEFAULT_TITLE,
    initial_dir: Path | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    runner: Runner | None = None,
) -> Path | None:
    """弹出系统文件夹选择对话框，返回选中的目录。

    Args:
        title: 对话框标题。
        initial_dir: 打开时的起始目录；不存在时忽略（tkinter 会自己找位置）。
        timeout_seconds: 等待上限，超时会杀掉子进程。
        runner: 执行方式；``None`` 表示真跑子进程。**测试必须注入替身**，否则会在
            CI 机器上弹出一个没人关的窗口。

    Returns:
        选中的绝对路径；用户取消（点了取消或关掉窗口）时返回 ``None``。

    Raises:
        FolderPickerBusyError: 已经有一个弹窗在等待操作。
        FolderPickerUnavailableError: 环境不支持（无图形环境 / 缺 tkinter / 启动失败）。
        FolderPickerTimeoutError: 超过 ``timeout_seconds`` 仍未关闭。
    """
    hint = _display_hint()
    if hint is not None:
        raise FolderPickerUnavailableError(
            f"{hint}。可改用网页内的「浏览…」逐级挑选（容器与无桌面的服务器只能用那个）"
        )
    if runner is None and not _tkinter_available():
        raise FolderPickerUnavailableError(
            "本机 Python 解释器缺少 tkinter，无法弹出系统对话框"
            "（Debian/Ubuntu 上装 python3-tk，Windows 用官方安装包重装并勾选 tcl/tk）。"
            "可改用网页内的「浏览…」逐级挑选"
        )

    execute = runner or _default_runner
    start = ""
    if initial_dir is not None and initial_dir.is_dir():
        start = str(initial_dir)
    argv = [sys.executable, "-c", _TK_SNIPPET, title, start]

    if not _ONE_AT_A_TIME.acquire(blocking=False):
        raise FolderPickerBusyError("已有一个文件夹选择弹窗在等待操作，请先处理它")
    try:
        try:
            completed = execute(argv, timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            # WHY 不回滚成「没选」：把「窗口还开在屏幕上」说成「你取消了」，会让用户
            # 在以为已经关掉的窗口里继续点，而服务端已经不再理会它了。
            raise FolderPickerTimeoutError(
                f"等待选择超过 {timeout_seconds:.0f} 秒，已放弃；屏幕上可能仍留着那个对话框"
            ) from exc
    finally:
        _ONE_AT_A_TIME.release()

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        raise FolderPickerUnavailableError(
            "系统对话框启动失败"
            + (f"：{detail[-1]}" if detail else f"（退出码 {completed.returncode}）")
            + "。可改用网页内的「浏览…」逐级挑选"
        )

    chosen = _parse_choice(completed.stdout)
    if not chosen:
        # 用户取消：这是正常结果，不是错误——把它当成失败会让界面弹一个红条，
    # 而用户什么都没做错。
        logger.info("用户在系统对话框里取消了选择")
        return None
    return Path(chosen).expanduser().resolve()


def _parse_choice(stdout: str) -> str:
    """从子进程输出里取出路径；取不到就返回空串（等价于取消）。

    WHY 容忍取不到：子进程可能因为平台差异多打印了别的东西（tkinter 的告警、Tcl 的提示）。
    按「最后一行是 JSON」解析，坏数据退化成取消，总好过把一个半截路径当作用户的选择。
    """
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return str(payload.get("path") or "")
    return ""


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TITLE",
    "FolderPickerBusyError",
    "FolderPickerTimeoutError",
    "FolderPickerUnavailableError",
    "pick_folder",
]
