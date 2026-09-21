"""Tier 2（docker 档位）真机验收。

验收项对应计划里写死的那三条，外加几条「本档位凭什么叫沙箱」的性质：

1. ``execute`` 真的在容器里跑通一次；
2. **中止后宿主 ``docker ps -a`` 无残留容器**；
3. 宿主无 Docker / 无执行镜像时**明确报错，不静默降级到更弱的档位**；
4. 工作区之外的路径不可见（含仓库根的 ``.env``——那正是 Tier 0 挡不住的东西）；
5. 网络确实切断；
6. 超时能真的停下容器并给出 ``timed_out``。

用法::

    python scripts/smoke_docker_sandbox.py
    python scripts/smoke_docker_sandbox.py --image harness-sandbox:latest

退出码：0 全部通过；1 有项目失败。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import AppConfig, ExecutionMode, SandboxTier  # noqa: E402
from runtime.execution_registry import abort_scope, bound_scope  # noqa: E402
from runtime.sandbox.docker_runner import DockerSandboxRunner  # noqa: E402
from runtime.sandbox.errors import SandboxPolicyError, SandboxUnavailableError  # noqa: E402
from runtime.sandbox.factory import build_sandbox_runner  # noqa: E402
from runtime.sandbox.models import CommandRequest  # noqa: E402

_NAME_PREFIX = "harness-sandbox-"
"""容器名前缀，与 runner 一致——「无残留」的判定靠它筛选。"""

_WORKSPACE = ROOT / "workspace"
_RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> bool:
    """记录并打印一条结论。"""
    _RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"：{detail}" if detail else ""))
    return ok


def _docker(args: list[str], timeout: float = 60.0) -> str:
    """调用 docker CLI，返回 stdout（失败返回空串）。"""
    try:
        done = subprocess.run(
            ["docker", *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return done.stdout.strip()


def _live_containers() -> list[str]:
    """与本次冒烟相关的残留容器（含已停止的）。"""
    out = _docker(["ps", "-a", "--filter", f"name={_NAME_PREFIX}", "--format", "{{.Names}}"])
    return [line for line in out.splitlines() if line.strip()]


def _config(**overrides: object) -> AppConfig:
    """构造只用于本脚本的配置（不读 .env，避免本机配置影响验收结论）。"""
    base: dict[str, object] = {
        "execution_mode": ExecutionMode.SANDBOX,
        "sandbox_tier": SandboxTier.DOCKER,
        "workspace": _WORKSPACE,
    }
    base.update(overrides)
    return AppConfig(**base)  # type: ignore[arg-type]


def _runner(image: str) -> DockerSandboxRunner:
    """按给定镜像构造 runner。"""
    return DockerSandboxRunner(
        workspace_root=_WORKSPACE, image=image, probe_timeout=30.0
    )


# --------------------------------------------------------------- 各段


def smoke_run(image: str) -> None:
    """第 1 段：容器内跑通一次，且工作区之外的路径不可见。"""
    print("\n=== 第 1 段：容器内执行 ===")
    runner = _runner(image)
    if not _check("能力探测通过", runner.probe(), runner.describe()):
        return

    result = runner.run(
        CommandRequest(
            command=(
                "echo MARKER_OK; python -c \"print('py', 6*7)\"; pwd; "
                "test -f /work/AGENTS.md && echo WORKSPACE_VISIBLE || echo WORKSPACE_MISSING; "
                "test -e /.env && echo ROOT_ENV_VISIBLE || echo ROOT_ENV_INVISIBLE; "
                "ls / | tr '\\n' ' '"
            ),
            cwd=_WORKSPACE,
            timeout=120,
        )
    )

    _check("命令在容器内跑通", result.exit_code == 0 and "MARKER_OK" in result.stdout,
           f"exit={result.exit_code}")
    _check("容器内 Python 可用", "py 42" in result.stdout)
    _check("容器内工作目录是 /work", "/work" in result.stdout, result.stdout.strip()[:80])
    # WHY 这条是本档位存在的理由：Tier 0 能读到仓库根的 .env，这里必须读不到。
    _check("仓库根的 .env 不可见", "ROOT_ENV_INVISIBLE" in result.stdout)
    _check("工作区内容可见", "WORKSPACE_VISIBLE" in result.stdout)
    if result.stderr.strip():
        print(f"        stderr: {result.stderr.strip()[:160]}")


def smoke_mount_scope(image: str) -> None:
    """第 2 段：工作区之外的 cwd 被拒绝（不放行、也不额外挂载）。"""
    print("\n=== 第 2 段：挂载范围 ===")
    runner = _runner(image)
    try:
        runner.run(CommandRequest(command="pwd", cwd=ROOT, timeout=60))
    except SandboxPolicyError as exc:
        _check("工作区之外的 cwd 被拒绝", True, str(exc)[:110])
        return
    _check("工作区之外的 cwd 被拒绝", False, "越界 cwd 竟然执行了")


def smoke_network(image: str) -> None:
    """第 3 段：网络确实切断。"""
    print("\n=== 第 3 段：网络 ===")
    runner = _runner(image)
    result = runner.run(
        CommandRequest(
            command=(
                "python -c \"import socket\n"
                "try:\n    socket.gethostbyname('example.com'); print('DNS_OK')\n"
                "except Exception as exc:\n    print('DNS_BLOCKED', type(exc).__name__)\""
            ),
            cwd=_WORKSPACE,
            timeout=120,
        )
    )
    _check("容器内无法解析域名", "DNS_BLOCKED" in result.stdout, result.stdout.strip()[:80])


def smoke_timeout(image: str) -> None:
    """第 4 段：超时真的停下容器。"""
    print("\n=== 第 4 段：超时 ===")
    runner = _runner(image)
    begin = time.perf_counter()
    result = runner.run(CommandRequest(command="sleep 60", cwd=_WORKSPACE, timeout=5))
    elapsed = time.perf_counter() - begin

    _check("超时被标记", result.timed_out is True, f"timed_out={result.timed_out}")
    _check("超时退出码为 124", result.exit_code == 124, f"exit={result.exit_code}")
    # WHY 时限放宽到 20 s 而不是贴着 5 s：容器启动本身就要约 0.7 s，超时后还有 kill 与
    # rm 的往返。这里要钉的是「没有一直等到 sleep 结束」，不是精确的调度延迟。
    _check("超时后及时返回", elapsed < 20, f"{elapsed:.1f} s")


def smoke_abort(image: str) -> None:
    """第 5 段：中止 → 命令停下、宿主无残留容器。"""
    print("\n=== 第 5 段：中止 ===")
    runner = _runner(image)
    scope = "smoke-docker-abort"
    outcome: dict[str, object] = {}

    def worker() -> None:
        with bound_scope(scope):
            outcome["result"] = runner.run(
                CommandRequest(command="sleep 60", cwd=_WORKSPACE, timeout=120)
            )

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    # 等容器真的起来再中止：否则测的是「还没开始就中止」，不能证明中止有效。
    deadline = time.time() + 30
    while time.time() < deadline and not _live_containers():
        time.sleep(0.2)

    begin = time.perf_counter()
    handles = abort_scope(scope)
    thread.join(timeout=30)
    elapsed = time.perf_counter() - begin

    _check("登记表里确实有句柄", handles >= 1, f"句柄数={handles}")
    _check("中止后命令及时返回", not thread.is_alive(), f"{elapsed:.2f} s")
    result = outcome.get("result")
    if result is None:
        _check("命令返回了结果", False, "工作线程没有产出结果")
    else:
        # WHY 断言 timed_out 为 False：中止与超时是两回事。若中止被记成超时，上游会把
        # 「用户主动停止」显示成「命令超时」，而两者后续该做的事完全不同。
        _check("中止不被误记为超时", result.timed_out is False, f"timed_out={result.timed_out}")
        _check("中止后退出码非 0", result.exit_code != 0, f"exit={result.exit_code}")


def smoke_no_leftovers(image: str) -> None:
    """第 6 段：宿主无残留容器。"""
    print("\n=== 第 6 段：残留 ===")
    leftovers = _live_containers()
    _check("宿主无本档位残留容器", not leftovers, "、".join(leftovers) or "（无）")


def smoke_fail_closed() -> None:
    """第 7 段：缺镜像 / 缺 docker 时明确报错，不静默降级。"""
    print("\n=== 第 7 段：失败即报错（不放宽档位语义） ===")

    missing_image = _runner("harness-nonexistent:latest")
    _check("镜像缺失时探测不通过", missing_image.probe() is False,
           missing_image.describe()[:110])

    missing_cli = DockerSandboxRunner(workspace_root=_WORKSPACE, docker_binary="docker-not-here")
    _check("docker CLI 缺失时探测不通过", missing_cli.probe() is False)

    try:
        build_sandbox_runner(_config(sandbox_docker_image="harness-nonexistent:latest"))
    except SandboxUnavailableError as exc:
        _check("装配层显式报错而非降级", True, str(exc)[:110])
    else:
        _check("装配层显式报错而非降级", False, "竟然装出了一个 runner")


def main() -> int:
    """跑完全部分段。"""
    parser = argparse.ArgumentParser(description="Tier 2 容器沙箱真机验收")
    parser.add_argument("--image", default="harness-sandbox:latest", help="执行镜像名")
    args = parser.parse_args()

    print(f"执行镜像：{args.image}；工作区：{_WORKSPACE}")
    existing = _live_containers()
    if existing:
        print(f"注意：跑之前已有 {len(existing)} 个同名容器（{existing}），结论可能受干扰。")

    for step in (
        lambda: smoke_run(args.image),
        lambda: smoke_mount_scope(args.image),
        lambda: smoke_network(args.image),
        lambda: smoke_timeout(args.image),
        lambda: smoke_abort(args.image),
        lambda: smoke_no_leftovers(args.image),
        smoke_fail_closed,
    ):
        try:
            step()
        except Exception as exc:  # noqa: BLE001
            # WHY 单段失败不中断：一段抛错（例如镜像缺失）不该让后面几段的结论都拿不到，
            # 而那几段恰好是排查「为什么这一段失败了」最需要的信息。
            _check(f"分段执行（{step}）", False, f"{type(exc).__name__}: {exc}")

    failed = [item for item in _RESULTS if not item[1]]
    print("\n=== 结论 ===")
    print(f"  {len(_RESULTS) - len(failed)}/{len(_RESULTS)} 项通过")
    for name, _, detail in failed:
        print(f"  未通过：{name} —— {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
