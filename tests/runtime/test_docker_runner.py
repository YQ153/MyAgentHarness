"""Tier 2 容器沙箱：参数拼装、探测与装配语义。

**这些用例刻意不依赖 Docker。** 它们的对象是「跑哪个约束组合」与「探测失败时会怎样」——
前者是本档位安全性的全部内容，后者是「档位语义不放宽」的落点，两者都是纯逻辑，不该
只在装了 Docker 的开发机上才能验证。真实容器的行为（跑通、断网、超时杀容器、中止无
残留）由 ``scripts/smoke_docker_sandbox.py`` 在真机上验（19 项），因为那类性质无法用替身
证明——这与 ``test_host_shell.py`` 的分工一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent.guardrails import build_interrupt_on
from config import NETWORK_MODE_HOST, NETWORK_MODE_NONE, ExecutionMode, SandboxTier
from runtime.sandbox import factory as factory_module
from runtime.sandbox.docker_runner import DockerSandboxRunner
from runtime.sandbox.errors import SandboxPolicyError, SandboxUnavailableError
from runtime.sandbox.models import CommandRequest, SandboxPolicy
from tests.conftest import make_config, make_root


def _runner(tmp_path: Path, **kwargs: Any) -> tuple[DockerSandboxRunner, Path]:
    """构造 runner，返回它与工作区目录。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return DockerSandboxRunner(workspace_root=workspace, **kwargs), workspace


def _request(workspace: Path, command: str = "echo hi", timeout: int = 30) -> CommandRequest:
    """构造一条指向工作区的命令请求。"""
    return CommandRequest(command=command, cwd=workspace, timeout=timeout)


def _argv(runner: DockerSandboxRunner, workspace: Path, **overrides: Any) -> list[str]:
    """取出 ``docker run`` 的完整参数（不含镜像与命令本身）。"""
    request = _request(workspace, **overrides)
    env = runner.policy.child_env(request.env)
    return runner._build_argv(request, runner._container_cwd(workspace), env, "test-name")  # noqa: SLF001


# --------------------------------------------------------------- 参数拼装


def test_mount_is_limited_to_the_workspace(tmp_path: Path) -> None:
    """**只挂载工作区**——这是本档位唯一的收益所在。

    WHY 断言「只有一个 -v」：多挂一个宿主目录，可见面当场回到 Tier 0 的形态，而界面上
    看不出任何区别（命令照样跑、日志照样正常）。
    """
    runner, workspace = _runner(tmp_path)

    argv = _argv(runner, workspace)

    mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "-v"]
    assert mounts == [f"{workspace}:/work"]
    assert argv[argv.index("-w") + 1] == "/work"


def test_read_only_mount_appends_ro(tmp_path: Path) -> None:
    """只读挂载要显式带上 ``:ro``（配置打开时）。"""
    runner, workspace = _runner(tmp_path, workspace_read_only=True)

    argv = _argv(runner, workspace)

    assert f"{workspace}:/work:ro" in argv


def test_network_follows_policy(tmp_path: Path) -> None:
    """网络策略映射到 ``--network``，默认 ``none``。"""
    default_runner, workspace = _runner(tmp_path)
    assert _argv(default_runner, workspace)[
        _argv(default_runner, workspace).index("--network") + 1
    ] == NETWORK_MODE_NONE

    host_runner, _ = _runner(tmp_path, policy=SandboxPolicy(network_mode=NETWORK_MODE_HOST))
    host_argv = _argv(host_runner, workspace)
    assert host_argv[host_argv.index("--network") + 1] == NETWORK_MODE_HOST


def test_resource_limits_and_hardening_are_applied(tmp_path: Path) -> None:
    """资源上限与加固项一个都不能少。"""
    runner, workspace = _runner(tmp_path)

    argv = _argv(runner, workspace)

    assert argv[argv.index("--memory") + 1] == f"{runner.policy.max_memory_mb}m"
    assert argv[argv.index("--cpus") + 1] == f"{runner.policy.cpu_percent / 100:.2f}"
    assert argv[argv.index("--pids-limit") + 1] == str(runner.policy.max_processes)
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in argv
    # --rm 与 --name：前者让正常退出不留痕，后者让中止能定位到容器
    assert "--rm" in argv
    assert argv[argv.index("--name") + 1] == "test-name"


def test_host_path_env_is_not_passed_into_the_container(tmp_path: Path) -> None:
    """**宿主的 ``PATH`` 不得进容器**——这条是本地实测踩出来的。

    WHY 值全对也可能全错：``SandboxPolicy.env_allowlist`` 是为宿主进程设计的，里面的
    ``PATH`` 在 Windows 宿主上取的是 ``C:\\...`` 一串；``-e`` 进 Linux 容器后会覆盖掉
    镜像里正确的 ``PATH``，于是 ``sleep`` / ``python`` 全找不到，命令以 127 立即返回。
    这个缺陷**只在「Windows 宿主 + Linux 容器」下出现**（正是 Docker Desktop 的默认形态），
    不报错、不告警，只会让人去怀疑镜像与命令本身。
    """
    runner, workspace = _runner(tmp_path)
    host_env = {"PATH": "C:\\Windows\\System32", "PATHEXT": ".EXE", "TZ": "UTC", "LANG": "C.UTF-8"}

    request = CommandRequest(command="echo hi", cwd=workspace, timeout=30, env=host_env)
    env = runner.policy.child_env(request.env)
    argv = runner._build_argv(request, "/work", env, "test-name")

    passed = {argv[index + 1].split("=", 1)[0] for index, item in enumerate(argv) if item == "-e"}
    assert passed == {"TZ", "LANG"}, f"不该透传的变量进了容器：{sorted(passed)}"


def test_container_shell_is_posix_sh(tmp_path: Path) -> None:
    """容器内用 ``sh -c``（最小镜像不保证有 bash），且命令原样传下去。"""
    runner, workspace = _runner(tmp_path)

    argv = _argv(runner, workspace, command="echo 'a  b'")

    assert argv[-3:] == ["sh", "-c", "echo 'a  b'"]


# --------------------------------------------------------------- 挂载范围


def test_cwd_outside_workspace_is_rejected(tmp_path: Path) -> None:
    """工作区之外的 cwd 直接报错，而不是额外挂一个目录迁就它。"""
    runner, _ = _runner(tmp_path)

    with pytest.raises(SandboxPolicyError, match="不在工作区内"):
        runner._container_cwd(tmp_path)  # noqa: SLF001


def test_subdirectory_maps_beneath_the_mount(tmp_path: Path) -> None:
    """工作区的子目录映射为挂载点下的相对路径。"""
    runner, workspace = _runner(tmp_path)
    nested = workspace / "src" / "app"
    nested.mkdir(parents=True)

    assert runner._container_cwd(nested) == "/work/src/app"  # noqa: SLF001
    assert runner._container_cwd(workspace) == "/work"  # noqa: SLF001


# --------------------------------------------------------------- 探测


def test_probe_fails_without_docker_cli(tmp_path: Path) -> None:
    """没有 docker 可执行文件时探测不通过。"""
    runner, _ = _runner(tmp_path, docker_binary="harness-definitely-not-here")

    assert runner.probe() is False
    assert "不可用" in runner.describe()


def test_probe_fails_when_image_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """镜像缺失时探测不通过，且错误信息指向构建脚本而不是让人去猜。"""
    runner, _ = _runner(tmp_path)

    def fake_cli(args: list[str], timeout: float | None = None) -> tuple[int, str, str]:
        del timeout
        if args[:1] == ["version"]:
            return 0, "28.1.1", ""
        if args[:1] == ["info"]:
            return 0, "linux", ""
        return 0, "", ""

    monkeypatch.setattr(runner, "_docker_cli", fake_cli)

    assert runner.probe() is False
    assert "setup_sandbox_image.py" in runner.describe()


def test_probe_rejects_windows_container_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 容器模式下必须探测失败。

    WHY：那时起 Linux 镜像的报错是「no matching manifest」，与「镜像不存在」长得一样，
    会把人引向完全错误的方向——探测先一步说清楚。
    """
    runner, _ = _runner(tmp_path)

    def fake_cli(args: list[str], timeout: float | None = None) -> tuple[int, str, str]:
        del timeout
        if args[:1] == ["version"]:
            return 0, "28.1.1", ""
        if args[:1] == ["info"]:
            return 0, "windows", ""
        return 0, "sha256:abc", ""

    monkeypatch.setattr(runner, "_docker_cli", fake_cli)

    assert runner.probe() is False
    assert "windows" in runner.describe()


def test_probe_caches_its_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """探测结果被缓存：一次装配里不该出现前后不一致的结论。"""
    runner, _ = _runner(tmp_path)
    calls: list[str] = []

    def fake_cli(args: list[str], timeout: float | None = None) -> tuple[int, str, str]:
        del timeout
        calls.append(args[0])
        if args[:1] == ["version"]:
            return 0, "28.1.1", ""
        if args[:1] == ["info"]:
            return 0, "linux", ""
        return 0, "sha256:abc", ""

    monkeypatch.setattr(runner, "_docker_cli", fake_cli)

    assert runner.probe() is True
    assert runner.probe() is True
    assert calls.count("version") == 1


# --------------------------------------------------------------- 装配语义


def test_docker_is_implemented_but_not_in_auto_order() -> None:
    """``docker`` 已实现，但**刻意不进 ``auto``**。

    WHY 这两件事必须分开：``auto`` 的既有候选之间差异是渐进的，而容器档位一次性改变
    三件事（网络被切断、宿主文件系统不可见、shell 换成 POSIX sh）。把它放进 ``auto``
    会让升级本版本的用户遇到「我的构建命令突然连不上网」，而那属于部署决定。
    """
    assert SandboxTier.DOCKER in factory_module._IMPLEMENTED_TIERS  # noqa: SLF001
    assert SandboxTier.DOCKER not in factory_module._AUTO_ORDER  # noqa: SLF001


def test_factory_fails_closed_when_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探测不过时**显式报错**，不静默降级到更弱的档位。"""
    monkeypatch.setattr(DockerSandboxRunner, "probe", lambda self: False)
    config = make_config(tmp_path, sandbox_tier=SandboxTier.DOCKER)
    # WHY 要建工作区目录：runner 的构造函数会校验它（挂载根不存在就无从挂载），
    # 不建的话这里抛的是 SandboxPolicyError，而本用例要钉的是「探测失败 → 不降级」。
    make_root(config).root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(SandboxUnavailableError):
        factory_module.build_sandbox_runner(config, workspace=make_root(config).root)


def test_factory_passes_config_to_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """装配时把配置项原样交给 runner（否则配了也没用）。"""
    captured: dict[str, Any] = {}

    class _Recorder:
        def __init__(self, policy: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

        def probe(self) -> bool:
            return True

        def describe(self) -> str:
            return "替身"

    monkeypatch.setattr(factory_module, "DockerSandboxRunner", _Recorder)
    config = make_config(
        tmp_path,
        sandbox_tier=SandboxTier.DOCKER,
        sandbox_docker_image="custom:1",
        sandbox_docker_workspace_read_only=True,
        sandbox_docker_user="1000:1000",
    )

    factory_module.build_sandbox_runner(config, workspace=make_root(config).root)

    assert captured["image"] == "custom:1"
    assert captured["workspace_read_only"] is True
    assert captured["user"] == "1000:1000"
    assert captured["workspace_root"] == make_root(config).root


# --------------------------------------------------------------- 审批联动


def test_interrupt_description_states_the_real_boundaries() -> None:
    """Tier 2 的审批提示必须写清容器内的真实边界，但**审批本身不放宽**。

    WHY 这是 T24 唯一被计划点名「最容易出安全问题」的地方：隔离缩小的是「命令碰到了
    会怎样」，不是「这条命令该不该跑」——一条 ``rm -rf /work`` 在容器里照样能删掉挂载
    进来的工作区。故这里同时断言两件事：审批仍在，且文案说清了边界。
    """
    interrupt_on = build_interrupt_on(ExecutionMode.SANDBOX, SandboxTier.DOCKER)

    assert "execute" in interrupt_on
    description = interrupt_on["execute"]["description"]  # type: ignore[index]
    assert "容器" in description
    assert "工作区" in description
    assert "网络" in description
    assert "宿主内核" in description


def test_docker_tier_approval_cannot_be_disabled_by_tier_choice() -> None:
    """档位本身不携带「免审批」语义——只有显式配置 ``require_approval=False`` 才行。"""
    assert "execute" in build_interrupt_on(ExecutionMode.SANDBOX, SandboxTier.DOCKER)
    assert build_interrupt_on(
        ExecutionMode.SANDBOX, SandboxTier.DOCKER, require_approval=False
    ) == {}
