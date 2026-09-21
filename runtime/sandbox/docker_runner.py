"""Tier 2：容器沙箱（Docker）。

**与 Tier 0/1 的根本差异：这是第一个真正限制「命令能看见什么」的档位。**

- Tier 0（宿主进程）：文件系统完全可见，一条 ``cat .env`` 能读到宿主凭据；
- Tier 1（WSL 发行版）：有独立文件系统，但仍可经 ``/mnt`` 读写宿主盘；
- Tier 2（容器）：默认只见镜像内容 + **显式挂载的工作区**，其余路径不存在。

由此带来本档位唯一一处「让安全更简单」的后果：凭据防护第一次有了结构性保障——仓库根的
``.env`` 在虚拟根之外，不挂载就看不见，不必再依赖路径规则（而路径规则在 ``execute`` 面前
本来就不成立，见 ``agent/guardrails.py``）。

**不宣称强隔离。** 容器共享宿主内核，以下明确不在防护范围内：内核漏洞逃逸、侧信道、
Docker 守护进程本身被攻破。本档位挡的是「命令失控的代价」：可见面、网络、进程树、资源。

**审批不放宽。** 隔离缩小的是「碰到了会怎样」，不是「这条命令该不该跑」——一条
``rm -rf /work`` 在容器里照样能删掉挂载进来的工作区。故 ``build_interrupt_on`` 在
``docker`` 档位下仍要求人工审批，只是描述文案改为写下容器内的真实边界。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from config import NETWORK_MODE_NONE, SandboxTier
from runtime import execution_registry
from runtime.sandbox.errors import SandboxError, SandboxPolicyError
from runtime.sandbox.models import CommandRequest, CommandResult, SandboxPolicy

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_TIMEOUT_EXIT_CODE = 124
"""超时退出码，与其余三个档位保持一致（``process_runner`` / ``wsl_runner`` / ``_winjob``）。"""

_CONTAINER_WORKSPACE = "/work"
"""工作区在容器内的挂载点。与 ``docker/sandbox.Dockerfile`` 的 ``WORKDIR`` 一致。"""

_DEFAULT_IMAGE = "harness-sandbox:latest"
"""默认执行镜像；由 ``scripts/setup_sandbox_image.py`` 构建。"""

_PROBE_TIMEOUT_SECONDS = 20.0
"""单条探测命令的超时。daemon 不可达时 ``docker`` 会自己重试很久，必须由我们兜住。"""

_CONTAINER_ENV_ALLOWLIST: frozenset[str] = frozenset({"TZ", "LANG"})
"""允许透传进容器的环境变量名（在 ``SandboxPolicy.child_env`` 之后再筛一层）。

WHY 需要第二层筛选：``SandboxPolicy.env_allowlist`` 那份白名单是为**宿主进程**
（Tier 0/1）设计的，其中 ``PATH`` / ``PATHEXT`` / ``SYSTEMROOT`` / ``SYSTEMDRIVE`` /
``WINDIR`` / ``COMSPEC`` / ``TEMP`` / ``TMP`` 在 Windows 宿主上取的是 **Windows 取值**。
把它们 ``-e`` 进 Linux 容器，等于用一串 ``C:\\...`` 覆盖掉镜像里正确的 ``PATH``——
容器随后连 ``sleep`` / ``python`` 都找不到，命令以**退出码 127** 立即返回。

**这个缺陷只在「Windows 宿主 + Linux 容器」这个组合下出现，而那恰好是 Docker Desktop
的默认形态。** 它不报错、不告警，只表现为「命令跑不起来」，排查时最容易被引向怀疑
镜像与命令本身（本地实测：加上 PATH 后 ``sleep 60`` 在 1.2 s 内以 127 结束，而手动
去掉 ``-e PATH`` 后同一命令正常）。

WHY 只留 ``TZ`` 与 ``LANG``：这两者的取值平台中立（时区名、locale 名），且确实影响
命令行为（时间戳、输出编码）；其余变量的正确取值只可能来自镜像自身。若用户把这两项
从 ``SANDBOX_ENV_ALLOWLIST`` 里去掉，这里也会跟着不传——上层配置仍然是唯一入口。
"""

_KILL_GRACE_SECONDS = 10.0
"""``docker kill`` 之后再等 ``docker run`` 退出的宽限时间。实测 kill 约 0.32 s。"""

_ABORT_RETRIES = 5
"""中止时的重试次数。

WHY 需要重试：从 ``Popen`` 到容器真正被创建之间有一个窗口，此时 ``docker kill`` 会
报「No such container」。只试一次就会留下一个「用户点了停止、命令却继续跑」的空档，
而它恰好落在最该可靠的那一刻。
"""

_ABORT_RETRY_DELAY_SECONDS = 0.2


class DockerSandboxRunner:
    """在一次性容器内执行命令。"""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        image: str = _DEFAULT_IMAGE,
        workspace_root: Path | None = None,
        workspace_read_only: bool = False,
        user: str = "",
        docker_binary: str = "docker",
        probe_timeout: float = _PROBE_TIMEOUT_SECONDS,
    ) -> None:
        """构造 runner。

        Args:
            policy: 资源与网络策略；``None`` 时使用默认策略。
            image: 执行镜像名。
            workspace_root: 宿主工作区根目录，**唯一**会被挂载进容器的路径；
                ``None`` 时以当前工作目录为准。
            workspace_read_only: 是否以只读方式挂载工作区。
            user: 传给 ``--user`` 的值（形如 ``1000:1000``）；空串表示用镜像默认身份。
            docker_binary: docker 可执行文件名。
            probe_timeout: 单条探测命令的超时秒数。

        Raises:
            SandboxPolicyError: ``workspace_root`` 不存在或不是目录。
        """
        self._policy = policy or SandboxPolicy()
        self._image = image.strip() or _DEFAULT_IMAGE
        self._docker = docker_binary
        self._probe_timeout = probe_timeout
        self._workspace_read_only = workspace_read_only
        self._user = user.strip()

        root = Path(workspace_root) if workspace_root is not None else Path.cwd()
        if not root.is_dir():
            msg = f"工作区目录不存在或不是目录：{root}"
            raise SandboxPolicyError(msg)
        self._workspace_root = root

        self._probe_lock = threading.Lock()
        self._probe_result: bool | None = None
        self._probe_detail = ""

    # ------------------------------------------------------------------ 协议

    @property
    def tier(self) -> SandboxTier:
        """本 runner 的沙箱档位。"""
        return SandboxTier.DOCKER

    @property
    def policy(self) -> SandboxPolicy:
        """当前生效的策略（只读）。"""
        return self._policy

    @property
    def image(self) -> str:
        """当前使用的执行镜像。"""
        return self._image

    def probe(self) -> bool:
        """探测本档位是否可用。

        WHY 结果要缓存：每项探测都是一次 CLI 往返（实测 daemon 查询数十毫秒、镜像查询
        数十毫秒），而装配层与 ``describe`` 可能各调一次；更重要的是**探测必须是只读的**，
        缓存顺带保证了「探测过就查过、不会中途变卦」，避免同一次装配里前后结论不一致。

        Returns:
            宿主与镜像都就绪时为 ``True``。
        """
        with self._probe_lock:
            if self._probe_result is not None:
                return self._probe_result
            ok, detail = self._probe()
            self._probe_result = ok
            self._probe_detail = detail
            return ok

    def describe(self) -> str:
        """返回一行档位说明（含实际生效的边界）。"""
        if not self.probe():
            return f"Tier 2 容器沙箱（不可用：{self._probe_detail}）"

        parts = [
            f"镜像 {self._image}",
            f"网络 {self._policy.network_mode}",
            f"工作区{'只读' if self._workspace_read_only else '读写'}挂载到 {_CONTAINER_WORKSPACE}",
            f"内存<={self._policy.max_memory_mb}MB",
            f"CPU<={self._policy.cpu_percent}%",
            f"进程数<={self._policy.max_processes}",
            "已丢弃全部 capabilities",
        ]
        if self._user:
            parts.append(f"身份 {self._user}")
        return "Tier 2 容器沙箱（" + "、".join(parts) + "）"

    def close(self) -> None:
        """释放资源；本 runner 无持久资源，空实现以保持接口一致。"""

    def run(self, request: CommandRequest) -> CommandResult:
        """在一次性容器内执行命令。

        Args:
            request: 命令请求。``cwd`` 必须落在工作区内——容器只挂载了工作区，
                工作区之外的目录在容器里不存在，放行只会得到一条难以理解的报错。

        Returns:
            执行结果；命令自身失败体现为退出码，不抛异常。超时为
            ``exit_code=124`` 且 ``timed_out=True``。

        Raises:
            SandboxPolicyError: 请求违反策略（含工作目录越界）。
            SandboxError: 沙箱自身故障（docker CLI 缺失、容器创建失败等）。
        """
        self._validate(request)
        env = self._policy.child_env(request.env)
        container_cwd = self._container_cwd(request.cwd)
        name = f"harness-sandbox-{uuid4().hex[:12]}"
        argv = self._build_argv(request, container_cwd, env, name)

        handle = _ContainerAbortHandle(name, self._docker, self._probe_timeout)
        execution_registry.register(handle)
        try:
            stdout, stderr, exit_code, timed_out = self._spawn(
                argv, request.timeout, handle, name
            )
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001
            # WHY 兜住所有异常：与 Tier 0 同一理由——沙箱故障不该以堆栈形式炸进
            # Agent 调用栈，统一转成带上下文的 SandboxError。
            logger.exception("容器沙箱执行失败：command=%s cwd=%s", request.command, request.cwd)
            msg = f"容器沙箱执行失败（{type(exc).__name__}）：{exc}"
            raise SandboxError(msg) from exc
        finally:
            execution_registry.unregister(handle)
            # WHY 无论成败都显式删除：``--rm`` 在**正常退出**时清理，但被 kill 的容器与
            # ``docker run`` 自身失败这两种情况下都可能留下痕迹。宿主无残留容器是本档位
            # 的验收项之一，而它只能靠这里保证。
            self._remove(name)

        if timed_out:
            exit_code = _TIMEOUT_EXIT_CODE

        stdout_text, stdout_clipped = _clip(stdout, request.max_output_bytes)
        stderr_text, stderr_clipped = _clip(stderr, request.max_output_bytes)

        return CommandResult(
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=exit_code,
            truncated=stdout_clipped or stderr_clipped,
            timed_out=timed_out,
            tier=self.tier,
        )

    # ------------------------------------------------------------------ 内部

    def _probe(self) -> tuple[bool, str]:
        """实际执行三段探测，返回 ``(是否可用, 说明或失败原因)``。"""
        if shutil.which(self._docker) is None:
            return False, f"找不到 {self._docker} 可执行文件"

        # WHY 是 ``.Server.Version`` 而不是 ``.ServerVersion``：Engine 28 的 ``docker
        # version`` 把服务端信息收在 ``Server`` 下，顶层没有 ``ServerVersion`` 字段。
        # 写错时模板求值失败、命令退出码非 0，于是**探测结果永远是「daemon 不可用」**——
        # 而本机 daemon 明明是好的（本地实测踩过）。
        code, server, err = self._docker_cli(["version", "--format", "{{.Server.Version}}"])
        if code != 0:
            # WHY 把 daemon 的原文带出来：daemon 没起、context 指错、权限不足三种情况的
            # 原文完全不同，而它们对使用者的下一步动作也完全不同。
            return False, f"docker daemon 不可用：{_first_line(err or server)}"
        server = server.strip() or "未知版本"

        # WHY 必须判 OSType：Windows 容器模式下起 Linux 镜像的报错是「no matching
        # manifest」，与「镜像不存在 / 需要拉取」长得一模一样，会把人引向完全错误的方向。
        code, ostype, _ = self._docker_cli(["info", "--format", "{{.OSType}}"])
        ostype = ostype.strip()
        if code == 0 and ostype and ostype != "linux":
            return False, (
                f"docker 处于 {ostype} 容器模式，本档位需要 linux 容器"
                "（Windows 宿主请确认 Docker Desktop 使用 Linux 后端）"
            )

        ok, detail = self._image_ready()
        if not ok:
            return False, detail
        return True, f"{detail}；daemon {server}（{ostype or '容器模式未知'}）"

    def _image_ready(self) -> tuple[bool, str]:
        """镜像是否已就绪。

        WHY 不用 ``docker image inspect <ref>``：本机（Docker Desktop 4.41 / Engine
        28.1.1 / overlayfs 存储驱动）实测该命令对**真实存在且能正常运行**的
        ``python:3.14-slim`` 返回 ``No such image``，而 ``docker image ls --filter``
        与 ``docker run --pull=never`` 都正常。用它当判据会把「镜像已就绪」误判成
        「镜像缺失」，从而把一个本来能跑的档位判死——**这是能力探测出错，与「档位语义
        不放宽」是两回事**（后者是有意报错）。
        """
        code, out, err = self._docker_cli(
            ["image", "ls", "--filter", f"reference={self._image}", "--format", "{{.ID}}"]
        )
        if code != 0:
            return False, f"查询执行镜像失败：{_first_line(err or out)}"
        if not out.strip():
            return False, (
                f"执行镜像 {self._image} 不在本机；"
                "先运行 python scripts/setup_sandbox_image.py 构建（详见 README「Docker 沙箱」）"
            )
        return True, f"镜像 {self._image} 已就绪"

    def _validate(self, request: CommandRequest) -> None:
        """校验请求与工作目录。"""
        if not isinstance(request, CommandRequest):
            msg = f"request 必须是 CommandRequest，当前为 {type(request).__name__}"
            raise SandboxPolicyError(msg)
        if not request.cwd.is_dir():
            msg = f"工作目录不存在或不是目录：{request.cwd}"
            raise SandboxPolicyError(msg)

    def _container_cwd(self, cwd: Path) -> str:
        """把宿主工作目录换算成容器内路径。

        WHY 越界即报错而不是「挂到 ``/`` 凑合」：容器只挂载工作区，工作区之外的目录在
        容器里根本不存在。若为了「让它能跑」而额外挂载宿主目录，本档位唯一的收益
        （限制可见面）就当场消失了——那正是 Tier 0 的形态。
        """
        root = self._workspace_root.resolve()
        try:
            relative = Path(cwd).resolve().relative_to(root)
        except ValueError as exc:
            msg = (
                f"命令的工作目录不在工作区内：{Path(cwd).resolve()}（工作区：{root}）。"
                "本档位只挂载工作区，其余路径在容器内不可见。"
            )
            raise SandboxPolicyError(msg) from exc

        # WHY 用 parts 判空而不是与 ``Path()`` 比较：``relative_to`` 在「就是根本身」时
        # 返回 ``Path('.')``，而 ``Path('.')`` 与 ``Path()`` 的相等性依赖实现细节；
        # ``.parts`` 为空才是「没有任何下层路径」的稳定表达。
        if not relative.parts:
            return _CONTAINER_WORKSPACE
        return f"{_CONTAINER_WORKSPACE}/{relative.as_posix()}"

    def _build_argv(
        self,
        request: CommandRequest,
        container_cwd: str,
        env: Mapping[str, str],
        name: str,
    ) -> list[str]:
        """拼装 ``docker run`` 参数。

        WHY 参数逐个写明而不是让用户配一堆额外参数：容器沙箱的价值全在「跑的是哪一个
        约束组合」上，把 ``--privileged`` 之类的开关透出去，等于给一个「看起来在沙箱里」
        的档位留一个后门；确有需要的部署应当改本文件，而不是改配置。
        """
        mount = f"{self._workspace_root}:{_CONTAINER_WORKSPACE}"
        if self._workspace_read_only:
            mount += ":ro"

        argv = [
            self._docker,
            "run",
            "--rm",
            # WHY 显式命名：中止只能靠名字定位容器（``docker kill`` 不能按 PID 作用）。
            "--name",
            name,
            "--network",
            "none" if self._policy.network_mode == NETWORK_MODE_NONE else "host",
            "-v",
            mount,
            "-w",
            container_cwd,
            "--memory",
            f"{self._policy.max_memory_mb}m",
            # WHY 换算成 --cpus：策略里的 cpu_percent 是百分比，docker 要的是核数。
            "--cpus",
            f"{self._policy.cpu_percent / 100:.2f}",
            "--pids-limit",
            str(self._policy.max_processes),
            # 丢弃全部 capabilities + 禁止提权。镜像里没有需要特权的操作（不装包、不提权），
            # 而默认 capability 集包含 CAP_NET_RAW / CAP_CHOWN 等，对本档位全是纯收益的削减。
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
        ]

        if self._user:
            argv += ["--user", self._user]

        for key, value in sorted(env.items()):
            # 见 _CONTAINER_ENV_ALLOWLIST：宿主的 PATH 等取值不能进容器。
            if key.upper() not in _CONTAINER_ENV_ALLOWLIST:
                continue
            argv += ["-e", f"{key}={value}"]

        # WHY 用 ``sh -c`` 而不是 ``bash -c``：最小镜像不保证有 bash，而 sh 是 POSIX 必备。
        # 需要 bash 特性的命令应当在命令里显式调用 bash（镜像里有时）。
        argv += [self._image, "sh", "-c", request.command]
        return argv

    def _spawn(
        self,
        argv: list[str],
        timeout: int,
        handle: _ContainerAbortHandle,
        name: str,
    ) -> tuple[str, str, int | None, bool]:
        """起容器并等待结果，返回 ``(stdout, stderr, exit_code, timed_out)``。

        WHY 用 ``Popen`` 而不用 ``subprocess.run``：超时与中止都要在**不终止 docker CLI**
        的前提下停掉容器。杀掉 CLI 不会停容器（容器归 daemon 管），而停掉容器会让 CLI
        自己退出——所以这里需要拿到进程句柄以便代它收尾。
        """
        proc = subprocess.Popen(  # noqa: S603  参数为列表，不经 shell
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            # WHY errors=replace：容器内输出可能是任意字节序列（二进制日志、错编文件），
            # 一次解码失败不该让整次执行以堆栈收场。
            errors="replace",
        )

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning("容器执行超时（%ss），正在终止：%s", timeout, name)
            handle.abort()
            try:
                stdout, stderr = proc.communicate(timeout=_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                # kill 后仍收不到退出：放弃等待，杀掉 CLI 本身并如实返回。容器由
                # finally 里的 ``docker rm -f`` 兜底，不会留在宿主上。
                proc.kill()
                stdout, stderr = proc.communicate()
                logger.error("容器终止后仍无退出，已强杀 docker CLI：%s", name)

        return stdout, stderr, proc.returncode, timed_out

    def _remove(self, name: str) -> None:
        """强制删除容器；不存在时静默返回。"""
        code, out, err = self._docker_cli(["rm", "-f", name], timeout=30.0)
        if code != 0 and "No such container" not in f"{out}{err}":
            logger.warning("清理容器 %s 失败：%s", name, _first_line(err or out))

    def _docker_cli(
        self, args: list[str], *, timeout: float | None = None
    ) -> tuple[int, str, str]:
        """调用 docker CLI，返回 ``(退出码, stdout, stderr)``；超时与缺失都按失败返回。"""
        try:
            completed = subprocess.run(  # noqa: S603  参数为列表，不经 shell
                [self._docker, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout if timeout is not None else self._probe_timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"超时（>{timeout or self._probe_timeout}s）"
        except FileNotFoundError:
            return 127, "", f"找不到 {self._docker} 可执行文件"
        return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


class _ContainerAbortHandle:
    """中止句柄：按容器名终止正在执行的命令。

    约定与 ``_winjob`` / ``host_shell`` 的句柄一致：可从任意线程调用，对已结束的容器
    是 no-op。实现上靠 ``docker kill`` —— 容器一停，正在等待的 ``docker run`` 会自己退出，
    因此这里**不需要**再去动 CLI 进程。
    """

    def __init__(self, name: str, docker_binary: str, timeout: float) -> None:
        self._name = name
        self._docker = docker_binary
        self._timeout = timeout
        self._lock = threading.Lock()
        self._aborted = False

    def abort(self) -> None:
        """终止容器；已结束或尚未创建时是 no-op。"""
        with self._lock:
            if self._aborted:
                return
            self._aborted = True

        for _ in range(_ABORT_RETRIES):
            code, out, err = self._call(["kill", self._name])
            if code == 0:
                return
            message = f"{out}{err}"
            # WHY「已停止」按 no-op 处理：句柄的契约是「对已结束的进程是 no-op」，而
            # ``docker kill`` 对一个已退出但尚未删除的容器会报 ``is not running``。
            # 把它记成告警会在每次「命令先自己结束、用户随后点停止」时留下一条噪音，
            # 而那种情形完全正常。
            if "is not running" in message:
                return
            if "No such container" in message:
                # 容器尚未创建完成（Popen 与创建之间有窗口），短暂等待后重试。
                time.sleep(_ABORT_RETRY_DELAY_SECONDS)
                continue
            logger.warning("中止容器 %s 失败：%s", self._name, _first_line(message))
            return

        logger.warning("中止容器 %s：容器始终未出现，命令可能尚未开始执行", self._name)

    def _call(self, args: list[str]) -> tuple[int, str, str]:
        """调用 docker CLI（中止路径专用，失败一律返回而不抛）。"""
        try:
            completed = subprocess.run(  # noqa: S603  参数为列表，不经 shell
                [self._docker, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return 1, "", f"{type(exc).__name__}: {exc}"
        return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """按字符数截断文本（口径与其余档位一致）。"""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _first_line(text: str) -> str:
    """取错误信息首行。

    WHY：docker CLI 的报错常带多行（如 use --help 提示、daemon 建议），而这里要放进
    一行档位说明或一条告警里；取首行才能保住「原因」本身不被截断到看不见。
    """
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""
