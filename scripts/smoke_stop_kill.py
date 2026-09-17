"""T2 风险真机验证：取消于工具执行中段时，子进程是否被终止。

背景：``RunService.stop()`` 置位取消事件后，``_stream_graph`` 会
``chunk_task.cancel()``，让取消沿着 ``graph.astream`` 传播。但 backend 的
``execute()`` 是**同步**调用（LangGraph 对同步节点使用线程池执行），
``task.cancel()`` 只能取消「等待线程结果」的 future，**无法中断已经在跑的
线程**。因此本脚本要实测的问题是：

    「运行已停止」之后，命令的子进程（含孙进程）还要多久才消失？

验证方式刻意不走真实模型：改用 ``asyncio.to_thread`` 复刻「同步 execute 跑在
工作线程 + 取消 future」这一机制（与 LangGraph 对同步节点的调度等价），
然后用带唯一标记的长命令 + 进程枚举，测量取消到进程消失的真实时延。

用法：

    uv run python scripts/smoke_stop_kill.py                  # sandbox 档位（Tier 0 Job Object）
    uv run python scripts/smoke_stop_kill.py --mode local     # local 档位（宿主子进程）
    uv run python scripts/smoke_stop_kill.py --timeout 20     # 缩短沙箱超时
    uv run python scripts/smoke_stop_kill.py --mode local --timeout 8 --settle 2 --ttl 20

关于 ``--ttl``：它决定父/孙进程自身的寿命。默认 600s 用于 sandbox 档位（要测
「命令活得比超时更久、最终被超时兜底回收」）。但 local 档位下 ``execute()`` 会
阻塞到孙进程寿终才返回（见文档中的机制说明），脚本的等待窗口会被撑到数百秒，
因此实测 local 时必须把 ``--ttl`` 压到 20s 量级，否则前台跑不完。

退出码：``0`` = 最终无残留（无论是否立即）；``1`` = 残留未被回收、
孙进程系自行退出（未被终止）、或验证出错。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# WHY 在 chdir 之后导入：这些模块会在导入时读配置与工作区路径，
# 顺序颠倒会让它们以「脚本所在目录」为基准解析相对路径。
from runtime.execution_registry import abort_scope, bound_scope  # noqa: E402
# WHY 强制 UTF-8 输出：Windows 控制台默认 GBK，日志里的档位说明含非 GBK 字符时
# print 会抛 UnicodeEncodeError，把一次成功的验证渲染成失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger("smoke_stop_kill")

_PS_LIST_COMMANDLINES = (
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
    "| Select-Object -ExpandProperty CommandLine"
)
"""枚举所有 python 进程的命令行。

WHY 用 PowerShell CIM 而不是 ``tasklist``：``tasklist`` 不输出命令行，
无法把本次验证的进程与机器上其他 python 进程区分开；而标记匹配是这里唯一
可靠的判据（父子进程都由同一个解释器启动）。
"""

_IMMEDIATE_KILL_SECONDS = 2.0
"""判定「取消即终止」的阈值：超过它说明进程是活到沙箱超时才被回收。"""

_SELF_EXIT_TOLERANCE_SECONDS = 3.0
"""判定「孙进程系自行退出」的时刻容差。

WHY 需要容差：进程退出与轮询发现之间存在秒级偏差，且父/孙的 sleep 计时各自独立，
精确相等不现实；容差取 3s 远小于参数校验保证的 ttl 与 timeout 间隔（≥10s），
不会把「超时被杀」误判成「自行退出」。
"""


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。

    Returns:
        含 ``mode`` / ``timeout`` / ``settle`` 的命名空间。
    """
    parser = argparse.ArgumentParser(description="T2 停止路径的子进程回收真机验证")
    parser.add_argument(
        "--mode",
        choices=["sandbox", "local"],
        default="sandbox",
        help="执行档位：sandbox=Tier 0 Job Object，local=宿主子进程（默认 sandbox）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=25,
        help="命令超时秒数（也是沙箱回收的兜底时限），默认 25",
    )
    parser.add_argument(
        "--settle",
        type=int,
        default=5,
        help="取消前等待子进程启动的稳定时间（秒），默认 5",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=600,
        help="父/孙进程自身的寿命秒数，默认 600；必须显著大于 --timeout",
    )
    parser.add_argument(
        "--no-abort",
        action="store_true",
        help="对照实验：只取消 future，不调用执行登记处的进程树终止（T7 之前的行为）",
    )
    args = parser.parse_args()
    if args.timeout < 5:
        parser.error("--timeout 不能小于 5，否则测不出「超时兜底」与「立即终止」的差别")
    if args.settle < 1:
        parser.error("--settle 不能小于 1")
    # WHY 强制 ttl 显著大于 timeout + settle：进程若逼近超时点自己退出，「进程
    # 最终消失」就只是寿终正寝，会被误判成「被成功回收」——这正是 local 档位
    # 最想暴露的假象；留出 10s 间隔才能把「被终止」与「自行退出」可靠区分开。
    if args.ttl < args.timeout + args.settle + 10:
        parser.error(
            "--ttl 需不小于 --timeout + --settle + 10，否则「被终止」与「自行退出」无法区分"
        )
    return args


def _python_command_lines() -> list[str]:
    """返回当前所有 python 进程的命令行。

    Returns:
        命令行字符串列表；进程不存在时为空列表。

    Raises:
        RuntimeError: PowerShell 调用失败（本机无 PowerShell 或 CIM 不可用）。
    """
    try:
        result = subprocess.run(  # noqa: S603, S607 固定命令，无外部输入拼接
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_LIST_COMMANDLINES],
            capture_output=True,
            text=True,
            # WHY 显式 UTF-8：Windows 控制台默认是 GBK，其他 python 进程的命令行
            # 里一旦有非 GBK 字符，解码报错会把「进程还在」误判成「进程没了」。
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"枚举 python 进程失败：{exc}"
        raise RuntimeError(msg) from exc

    if result.returncode != 0:
        msg = f"PowerShell 枚举进程返回 {result.returncode}：{result.stderr.strip()}"
        raise RuntimeError(msg)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _alive(marker: str) -> bool:
    """判断带指定标记的进程是否仍在运行。

    Args:
        marker: 写进命令行的唯一标记。

    Returns:
        存在匹配进程返回 ``True``。
    """
    return any(marker in line for line in _python_command_lines())


def _build_command(parent_marker: str, child_marker: str, *, ttl: int) -> str:
    """构造「父进程 + 孙进程」的长命令，两级都带可识别标记。

    WHY 必须造一个孙进程：只杀直接子进程而放过孙进程，是进程树回收最典型的
    漏网形态（``process.kill()`` 就只杀一层）；Job Object / 进程组的价值正是
    覆盖这一层。

    Args:
        parent_marker: 父进程命令行标记。
        child_marker: 孙进程命令行标记。
        ttl: 父/孙进程自身寿命秒数。

    Returns:
        可直接交给 shell 的命令字符串。
    """
    child_code = f"import time; time.sleep({ttl})  # {child_marker}"
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"time.sleep({ttl})  # {parent_marker}"
    )
    # WHY 用 sys.executable 绝对路径而不是裸 ``python``：沙箱子进程的环境是
    # 白名单清洗过的，PATH 里未必有可解析的 python，绝对路径才稳定。
    return f'"{sys.executable}" -c "{parent_code}"'


async def _await_until(predicate: Any, *, timeout: float, interval: float = 0.5) -> float | None:
    """异步轮询等待条件成立。

    WHY 必须 async：``asyncio.to_thread`` 提交的任务要等事件循环轮转才真正
    落到线程池；若这里用阻塞的 ``time.sleep`` 轮询，循环被占死、任务压根
    不会启动，验证就会变成「命令没跑起来」的假失败。

    Args:
        predicate: 无参、返回布尔的可调用对象。
        timeout: 最长等待秒数。
        interval: 轮询间隔秒数。

    Returns:
        从调用到条件成立的秒数；超时返回 ``None``。
    """
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        if predicate():
            return time.monotonic() - started
        await asyncio.sleep(interval)
    return None


def _build_backend(config: Any) -> Any:
    """按配置构造 backend（含 ``/memories/`` 路由所需的 store）。

    Args:
        config: 应用配置。

    Returns:
        ``CompositeBackend``。

    Raises:
        RuntimeError: 依赖缺失或工作区不可用。
    """
    try:
        from langgraph.store.memory import InMemoryStore

        from agent.backends import build_backend
    except ImportError as exc:  # pragma: no cover - 依赖缺失属环境故障
        msg = f"构造 backend 所需依赖不可用：{exc}"
        raise RuntimeError(msg) from exc

    # WHY 用内存 store：本脚本只验证命令执行的进程回收，不触及长期记忆，
    # 引入真实 store 只会把 SQLite 的初始化问题混进验证结论。
    return build_backend(config, InMemoryStore())


async def _measure(
    backend: Any,
    *,
    timeout: int,
    settle: int,
    ttl: int,
    abort: bool = True,
) -> dict[str, Any]:
    """执行一次「启动长命令 → 取消 → 测量进程存活」的验证。

    Args:
        backend: 提供 ``execute`` 的 backend。
        timeout: 命令超时秒数。
        settle: 取消前等待子进程出现的秒数。
        ttl: 父/孙进程自身寿命秒数，用于识别「自行退出」与「被终止」。
        abort: 是否模拟 T7 的进程层确认（``abort_scope``）；``False`` 为
            对照实验，只取消 future 而不终止进程树。

    Returns:
        含标记与各项时延的字典。

    Raises:
        RuntimeError: 子进程未能在预期时间内启动（验证前提不成立）。
    """
    tag = uuid.uuid4().hex[:8]
    parent_marker = f"MKP-{tag}"
    child_marker = f"MKC-{tag}"
    command = _build_command(parent_marker, child_marker, ttl=ttl)

    if _alive(parent_marker) or _alive(child_marker):
        msg = "标记冲突：同标记进程已存在，验证结果不可信"
        raise RuntimeError(msg)

    state: dict[str, Any] = {"finished": None, "exit_code": None, "error": None}

    def _execute() -> None:
        """在工作线程里跑同步 execute，并记录真实返回时刻与输出。"""
        try:
            # WHY 绑到执行登记处的作用域：真实链路里 ``RunService._consume``
            # 会把本次运行绑定到会话 ID，命令执行器据此登记进程树句柄。
            # 这里用 tag 当作用域，使下面的 ``abort_scope`` 与真实 stop 等价。
            with bound_scope(tag):
                response = backend.execute(command, timeout=timeout)
            state["exit_code"] = response.exit_code
            # WHY 存输出：命令若压根没起来（引号被吞、沙箱拒绝执行等），
            # 唯一线索就是这段文本；不留下来就只能看到「子进程未启动」。
            state["output"] = (response.output or "")[:500]
        except Exception as exc:  # noqa: BLE001 记录后由外层判定
            logger.exception("execute 抛出异常")
            state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            state["finished"] = time.monotonic()

    task = asyncio.ensure_future(asyncio.to_thread(_execute))
    try:
        # WHY 先让出一次循环：确保 to_thread 已把 _execute 交给线程池，
        # 否则下面的轮询会在「命令还没启动」的状态上空转。
        await asyncio.sleep(0)
        # WHY 一次枚举同时判两个标记：``_alive`` 每次调用都要起一次 PowerShell
        # CIM 查询（秒级开销），逐标记判断会让每轮轮询的耗时翻倍，在短窗口下
        # 足以把「进程已启动」误判成「未启动」——sandbox 回归就栽在这里。
        def _both_alive() -> bool:
            lines = _python_command_lines()
            return all(
                any(marker in line for line in lines)
                for marker in (parent_marker, child_marker)
            )

        appeared = await _await_until(
            _both_alive,
            # WHY 窗口覆盖至少数轮枚举：CIM 查询本身是秒级的，窗口太小会把
            # 「还没查完」当成「进程没起来」。
            timeout=settle + 15,
        )
        if appeared is None:
            # WHY 失败前先等线程收尾：命令启动失败的原因（引号被吞、沙箱拒绝）
            # 只会出现在 execute 的返回值里，直接抛错会让排查失去唯一线索。
            await _await_until(lambda: state["finished"] is not None, timeout=10)
            msg = (
                f"子进程（含孙进程）未在 {settle + 15}s 内启动，验证前提不成立；"
                f"exit_code={state['exit_code']} error={state['error']} "
                f"output={state.get('output')!r}"
            )
            raise RuntimeError(msg)
        proc_started = time.monotonic()
        logger.info("子进程与孙进程均已启动（%.1fs）", appeared)

        # 稳定一段时间，确保取消发生在「工具执行中段」而非启动瞬间
        await asyncio.sleep(settle)

        cancelled_at = time.monotonic()
        if abort:
            # WHY 先终止进程树再取消 future：真实 ``stop()`` 正是这个顺序，
            # 反过来会让「命令刚被杀、future 才取消」之间的输出丢失。
            logger.info("模拟 RunService.stop()：按会话终止在跑的进程树")
            aborted = abort_scope(tag)
            logger.info("已向 %d 个在跑命令发出终止", aborted)
        logger.info("模拟 RunService.stop()：取消等待线程结果的 future")
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    parent_death: float | None = None
    child_death: float | None = None
    deadline = time.monotonic() + timeout + 30
    while time.monotonic() < deadline and (parent_death is None or child_death is None):
        if parent_death is None and not _alive(parent_marker):
            parent_death = time.monotonic() - cancelled_at
        if child_death is None and not _alive(child_marker):
            child_death = time.monotonic() - cancelled_at
        if parent_death is None or child_death is None:
            await asyncio.sleep(1.0)

    residual = _alive(parent_marker) or _alive(child_marker)
    # WHY 单看「进程消失」会把「自己寿终」误判成「被成功回收」：孙进程若活到
    # 自身 ttl 才退出，说明取消压根没波及它。用消失时刻是否贴住 ttl 到期点来区分。
    child_death_at = None if child_death is None else cancelled_at + child_death
    child_self_exit = child_death_at is not None and abs(
        child_death_at - (proc_started + ttl)
    ) <= _SELF_EXIT_TOLERANCE_SECONDS

    # 等线程真正收尾：execute 返回后才能确认沙箱侧的清理已完成
    execute_return: float | None = None
    execute_pending = False
    if state["finished"] is not None:
        execute_return = state["finished"] - cancelled_at
    elif not residual:
        waited = await _await_until(lambda: state["finished"] is not None, timeout=timeout + 30)
        if waited is not None and state["finished"] is not None:
            execute_return = state["finished"] - cancelled_at
        else:
            execute_pending = True
    else:
        # WHY 有残留时不再等待：local 档位的 execute 阻塞在无超时的 communicate()
        # （孙进程持有管道写端），只有孙进程寿终才会返回——继续等只会把脚本挂住
        # 数百秒，而「残留」这个结论此刻已经拿到了。
        execute_pending = True
        logger.warning(
            "进程仍有残留，放弃等待 execute 返回：local 档位会阻塞到子进程寿终（管道写端未关闭）"
        )

    return {
        "tag": tag,
        "parent_marker": parent_marker,
        "child_marker": child_marker,
        "cancel_to_parent_death": parent_death,
        "cancel_to_child_death": child_death,
        "cancel_to_execute_return": execute_return,
        "exit_code": state["exit_code"],
        "error": state["error"],
        "residual": residual,
        "child_self_exit": child_self_exit,
        "execute_pending": execute_pending,
    }


def _format_seconds(value: float | None) -> str:
    """格式化秒数；``None`` 显示为「未回收」。

    Args:
        value: 秒数或 ``None``。

    Returns:
        可读字符串。
    """
    return "未回收" if value is None else f"{value:.1f}s"


def _report(result: dict[str, Any], *, mode: str, timeout: int) -> int:
    """输出验证结论并返回退出码。

    Args:
        result: ``_measure`` 的返回值。
        mode: 执行档位。
        timeout: 命令超时秒数。

    Returns:
        最终无残留返回 ``0``；残留返回 ``1``。
    """
    print("\n--- 测量结果 ---")
    print(f"档位                     : {mode}（命令超时 {timeout}s）")
    print(f"标记                     : {result['tag']}")
    print(f"取消 → 父进程消失        : {_format_seconds(result['cancel_to_parent_death'])}")
    print(f"取消 → 孙进程消失        : {_format_seconds(result['cancel_to_child_death'])}")
    print(f"取消 → execute 返回      : {_format_seconds(result['cancel_to_execute_return'])}")
    print(f"execute 退出码 / 异常    : {result['exit_code']} / {result['error']}")
    print(f"观察窗口结束时仍有残留   : {result['residual']}")
    print(f"孙进程系自行退出（非被杀）: {result['child_self_exit']}")
    print(f"execute 未返回（线程占死）: {result['execute_pending']}")

    print("\n--- 结论 ---")
    if result["residual"]:
        print("FAIL：停止后子进程最终未被回收，存在失控进程残留")
        return 1
    if result["child_self_exit"]:
        print(
            "FAIL：孙进程是活到自己寿终才消失，不是被终止——取消未波及进程树，"
            "「最终无残留」只是它自己退出的假象"
        )
        return 1

    deaths = [
        value
        for value in (result["cancel_to_parent_death"], result["cancel_to_child_death"])
        if value is not None
    ]
    slowest = max(deaths) if deaths else 0.0
    if slowest <= _IMMEDIATE_KILL_SECONDS:
        print(f"PASS：取消后 {slowest:.1f}s 内整棵进程树即被终止（含孙进程）")
    else:
        print(
            f"PASS（有残留窗口）：整棵进程树最终被回收，但不是取消即终止——"
            f"最长存活 {slowest:.1f}s，与命令超时（{timeout}s）一致，"
            "说明靠沙箱超时兜底（Job Object TerminateJobObject / 进程组 SIGKILL），"
            "而非取消信号即时传达"
        )
    return 0


def main() -> int:
    """执行验证并输出结论。

    Returns:
        进程最终无残留返回 ``0``；残留或出错返回 ``1``。
    """
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # WHY 通过环境变量改配置而不是手动拼 AppConfig：档位选择、runner 装配与
    # 超时都走的是生产同一条配置链路，改环境变量才能保证验证的档位与线上一致。
    os.environ["EXECUTION_MODE"] = args.mode
    os.environ["SANDBOX_TIER"] = "process"
    os.environ["SANDBOX_TIMEOUT"] = str(args.timeout)
    os.environ["SHELL_TIMEOUT"] = str(args.timeout)

    from config import AppConfig

    config = AppConfig.load()
    print(
        f"[cfg] mode={config.execution_mode.value} tier={config.sandbox_tier.value} "
        f"timeout={args.timeout}s workspace={config.workspace}"
    )

    try:
        backend = _build_backend(config)
        describe = getattr(backend, "describe", None)
        desc = describe() if callable(describe) else "（该档位无 describe）"
        print(f"[backend] id={getattr(backend, 'id', '?')} desc={desc}")

        result = asyncio.run(
            _measure(
                backend,
                timeout=args.timeout,
                settle=args.settle,
                ttl=args.ttl,
                abort=not args.no_abort,
            )
        )
    except Exception as exc:  # noqa: BLE001 验证脚本的顶层收敛：出错即判失败
        logger.exception("验证执行失败")
        print(f"\nFAIL：验证未能完成（{type(exc).__name__}）：{exc}")
        return 1

    code = _report(result, mode=args.mode, timeout=args.timeout)
    if not result["execute_pending"]:
        return code

    # WHY os._exit：工作线程仍阻塞在 communicate()，而 asyncio 默认线程池的线程
    # 会在解释器退出时被 join——正常 sys.exit 会把脚本再挂住数百秒（正是 local
    # 档位的故障表现本身）。结论已经打印完毕，显式 flush 后直接终止进程。
    logger.warning("execute 仍未返回，跳过线程回收直接退出（工作线程阻塞在管道读取）")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
