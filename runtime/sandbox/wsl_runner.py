"""Tier 1：WSL 发行版沙箱。

隔离强度高于 Tier 0——命令跑在 WSL2 的 utility VM 内的 Linux 发行版里，
与宿主 Windows 之间隔着 VM 边界和一套独立的 Linux 权限模型，资源上限由
Linux rlimit 施加而非 Windows Job Object。

有三件事**只能在 Linux 侧完成**，它们共同决定了本模块的实现形态：

1. **进程树终止**：杀掉 Windows 侧的 ``wsl.exe`` 只是断开了一条连接，VM 内
   的进程并不随之消失。因此超时必须由 Linux 侧的 GNU ``timeout`` 终止整个
   进程组来完成，Windows 侧的等待只是兜底。
2. **资源上限**：Linux 进程不是 Windows 进程，Job Object 管不到它们，
   只能用 ``ulimit`` 在 shell 里设置后由子进程继承。
3. **路径语义**：工作区在 Windows 侧、命令在 Linux 侧，工作目录必须显式
   换算成 ``/mnt/<盘符>/...``，否则 ``cd`` 到的是一个不存在的路径。

**本档位仍然不是安全边界**：发行版是持久环境（命令留下的文件与安装的包会
一直存在），且通过 ``/mnt`` 仍能读写宿主文件。因此它同样必须与 HITL 人工
审批配合使用，参见 ``agent/guardrails.py``。
"""

from __future__ import annotations

import io
import logging
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from config import SandboxTier
from runtime.sandbox.errors import SandboxError, SandboxPolicyError, SandboxUnavailableError
from runtime.sandbox.models import CommandRequest, CommandResult, SandboxPolicy

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_WSL_EXE = "wsl.exe"

_TIMEOUT_EXIT_CODE = 124
"""GNU ``timeout`` 的超时退出码，与 ``LocalShellBackend`` 的约定保持一致。"""

_TIMEOUT_MARKER = "__WSL_SANDBOX_TIMEOUT__"
"""超时哨兵。

WHY 不只看退出码：``timeout -s KILL`` 杀死命令后返回 137 而非 124，而
137 也完全可能是命令自己被 SIGKILL 的结果。由 Linux 侧在 stderr 里打一个
哨兵，才能让「是不是超时」这件事不受退出码语义漂移的影响。
"""

_GRACE_SECONDS = 5
"""Windows 侧等待的宽限期；Linux 侧超时后留给 wsl.exe 收尾的时间。"""

_STREAM_JOIN_SECONDS = 10
"""等待输出读线程的最长时间。"""

_READ_CHUNK = 65_536

_PROBE_TIMEOUT_SECONDS = 30
"""单次发行版探测的超时；冷启动一个发行版可能要几秒。"""

_LINUX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
"""Linux 侧显式覆盖的 PATH。

WHY 显式覆盖：既保证命令可用（发行版自身的 PATH 未必包含标准目录），也
挡住宿主 Windows 的 PATH 经 WSLENV 渗进沙箱。
"""

_WSL_NOISE_PREFIX = "wsl:"
"""``wsl.exe`` 自身诊断信息的前缀。"""

_JOB_NOTICE_PATTERN = re.compile(r"^bash: line \d+:\s*\d+ (Killed|Terminated|Stopped)\b")
"""bash 的作业控制通知，例如 ``bash: line 1: 392 Killed timeout ...``。

WHY 也要过滤：这类通知是 bash 在报告「它替我们杀掉了超时命令」，属于沙箱
自身的行为回执，混进命令的 stderr 后会被模型当成命令的失败原因来排查。
"""

_PROBE_OK_TOKEN = b"WSL_SANDBOX_PROBE_OK"


class _CapturedOutput(NamedTuple):
    """一次执行采集到的原始输出。"""

    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    exit_code: int | None
    timed_out: bool


class WslSandboxRunner:
    """在 WSL 发行版内执行命令并施加 Linux 侧的资源约束。"""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        distro: str | None = None,
        probe_timeout: int = _PROBE_TIMEOUT_SECONDS,
    ) -> None:
        """构造 runner。

        Args:
            policy: 资源与隔离策略；``None`` 时使用默认策略。
            distro: 指定发行版名称；``None`` 时自动挑选首个满足要求的发行版。
            probe_timeout: 单次探测的超时秒数，非法值回退为默认值。

        Raises:
            ValueError: ``distro`` 不是字符串或 ``None``。
        """
        if distro is not None and not isinstance(distro, str):
            msg = f"distro 必须是 str 或 None，当前为 {type(distro).__name__}"
            raise ValueError(msg)

        self._policy = policy or SandboxPolicy()
        self._requested_distro = distro.strip() if distro and distro.strip() else None
        self._probe_timeout = (
            probe_timeout if isinstance(probe_timeout, int) and probe_timeout > 0 else _PROBE_TIMEOUT_SECONDS
        )
        self._resolved_distro: str | None = None
        self._probed = False
        # WHY 加锁：Web 服务下多个请求会并发调用 ``run``，而发行版探测涉及
        # 多次 ``wsl.exe`` 调用，重复探测既慢又可能选出两个不同的发行版。
        self._lock = threading.Lock()

    @property
    def tier(self) -> SandboxTier:
        """本 runner 的沙箱档位。"""
        return SandboxTier.WSL

    @property
    def policy(self) -> SandboxPolicy:
        """当前生效的策略（只读）。"""
        return self._policy

    @property
    def distro(self) -> str | None:
        """已探测通过的发行版名称；未探测时为 ``None``。"""
        return self._resolved_distro

    def probe(self) -> bool:
        """探测是否存在满足要求的 WSL 发行版。

        要求：发行版内有 ``bash`` 与 ``timeout``，且挂载了 ``/mnt``——三者缺一，
        本档位的核心保证（shell 执行、超时终止、访问工作区）就无从谈起。
        """
        if sys.platform != "win32":
            logger.warning("WSL 档位仅在 Windows 上可用，当前平台：%s", sys.platform)
            return False

        with self._lock:
            if not self._probed:
                self._resolved_distro = self._discover_distro()
                self._probed = True
        return self._resolved_distro is not None

    def describe(self) -> str:
        """返回一行档位说明。"""
        distro = self._resolved_distro or self._requested_distro or "自动选择"
        return (
            f"Tier 1 WSL 沙箱（发行版 {distro}：Linux rlimit 进程数"
            f"<={self._policy.max_processes}、内存<={self._policy.max_memory_mb}MB、"
            f"CPU 时间按超时×{self._policy.cpu_percent}% 折算，"
            "超时由 GNU timeout 终止整个进程组）"
        )

    def close(self) -> None:
        """释放资源；本 runner 不持有跨调用的资源，空实现以保持接口一致。"""

    def run(self, request: CommandRequest) -> CommandResult:
        """在 WSL 发行版内执行一条命令。

        Args:
            request: 命令请求。

        Returns:
            执行结果；命令自身失败体现为退出码，不抛异常。

        Raises:
            SandboxPolicyError: 请求违反策略（类型不对、工作目录不存在）。
            SandboxUnavailableError: 没有可用的 WSL 发行版。
            SandboxError: 沙箱自身故障（如 ``wsl.exe`` 无法启动）。
        """
        self._validate(request)
        distro = self._require_distro()
        cwd_wsl = _to_wsl_path(request.cwd)
        env = self._policy.child_env(request.env)
        argv = [
            _WSL_EXE,
            "--distribution",
            distro,
            # WHY 必须用 ``--exec`` 而不是 ``--``：后者会让 wsl.exe 把参数再
            # 交给一层 shell 解析，命令里的 ``$(...)`` 会在错误的位置被展开、
            # ``;`` 会把一条命令切成多条（实测 ``timeout X; echo $?`` 恒得
            # 0，因为两条命令各自在新的上下文里执行）。``--exec`` 是直接
            # execve，参数原样进入 argv，命令语义才与它在真 Linux 上一致。
            "--exec",
            "bash",
            "-c",
            self._build_linux_command(request, cwd_wsl),
        ]

        logger.info("WSL 执行开始：distro=%s cwd=%s timeout=%ss", distro, cwd_wsl, request.timeout)
        try:
            # WHY 用参数列表而非字符串：命令里可能含空格、引号与 shell 元字符，
            # 交给 subprocess 逐个参数转义，避免 Windows 侧的引号解析踩坑。
            with subprocess.Popen(
                argv,
                cwd=str(request.cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process:
                captured = self._collect_output(process, request)
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("WSL 沙箱执行失败：distro=%s command=%s", distro, request.command)
            msg = f"WSL 沙箱执行失败（{type(exc).__name__}）：{exc}"
            raise SandboxError(msg) from exc

        stdout_text = _decode(captured.stdout)
        stderr_text = _strip_noise(_decode(captured.stderr))
        timed_out = captured.timed_out

        if _TIMEOUT_MARKER in stderr_text:
            timed_out = True
            stderr_text = "\n".join(
                line for line in stderr_text.split("\n") if _TIMEOUT_MARKER not in line
            )

        logger.info(
            "WSL 执行结束：distro=%s exit=%s timed_out=%s truncated=%s",
            distro,
            captured.exit_code,
            timed_out,
            captured.stdout_truncated or captured.stderr_truncated,
        )
        return CommandResult(
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=captured.exit_code,
            truncated=captured.stdout_truncated or captured.stderr_truncated,
            timed_out=timed_out,
            tier=self.tier,
        )

    def _validate(self, request: CommandRequest) -> None:
        """校验请求与工作目录。"""
        if not isinstance(request, CommandRequest):
            msg = f"request 必须是 CommandRequest，当前为 {type(request).__name__}"
            raise SandboxPolicyError(msg)
        if not request.cwd.is_dir():
            msg = f"工作目录不存在或不是目录：{request.cwd}"
            raise SandboxPolicyError(msg)

    def _require_distro(self) -> str:
        """返回可用的发行版，不可用时报错而非降级。"""
        if not self.probe():
            msg = (
                "WSL 沙箱不可用：未找到满足要求的发行版"
                "（需要发行版内存在 bash、timeout，且挂载了 /mnt）。"
                "请安装发行版，或用 SANDBOX_WSL_DISTRO 指定一个。"
            )
            raise SandboxUnavailableError(msg)
        distro = self._resolved_distro
        if not distro:
            msg = "WSL 发行版解析结果为空，拒绝执行以避免在未知环境中运行命令"
            raise SandboxUnavailableError(msg)
        return distro

    def _build_linux_command(self, request: CommandRequest, cwd_wsl: str) -> str:
        """拼接在 Linux 侧执行的完整命令串。

        形态：设置 PATH → 进入工作目录 → 施加 rlimit → ``timeout`` 包裹命令
        → 按退出码打超时哨兵。
        """
        cpu_seconds = max(1, request.timeout * self._policy.cpu_percent // 100)
        memory_kb = self._policy.max_memory_mb * 1024
        steps = [
            f"export PATH={shlex.quote(_LINUX_PATH)}",
            f"cd {shlex.quote(cwd_wsl)} || exit 1",
            # WHY ulimit 失败只告警、不中断执行：部分发行版或嵌套环境不允许
            # 调低某个 rlimit，为「限制没设上」就拒绝整条命令，代价与收益
            # 不成比例——但必须让人看得见，所以写进 stderr 而不是丢弃。
            (
                f"ulimit -u {self._policy.max_processes} 2>/dev/null "
                "|| echo '[sandbox] 无法设置进程数上限' >&2"
            ),
            (
                f"ulimit -v {memory_kb} 2>/dev/null "
                "|| echo '[sandbox] 无法设置内存上限' >&2"
            ),
            (
                f"ulimit -t {cpu_seconds} 2>/dev/null "
                "|| echo '[sandbox] 无法设置 CPU 时间上限' >&2"
            ),
            f"timeout -s KILL {request.timeout} bash -c {shlex.quote(request.command)}",
            (
                'rc=$?; case "$rc" in 124|137) '
                f"echo {shlex.quote(_TIMEOUT_MARKER)} >&2;; esac; exit $rc"
            ),
        ]
        return "; ".join(steps)

    def _collect_output(
        self,
        process: subprocess.Popen[bytes],
        request: CommandRequest,
    ) -> _CapturedOutput:
        """等待进程结束并采集有界输出。

        WHY 用两个读线程而不是 ``communicate``：``communicate`` 会把全部输出
        读进内存，一条 ``yes`` 命令就能吃光宿主机；这里只保留阈值内的字节，
        超出部分继续读走丢弃，既不阻塞子进程，内存占用也有上界。
        """
        limit = request.max_output_bytes
        slots: dict[str, tuple[bytes, int]] = {}
        workers: list[threading.Thread] = []

        def reader(name: str, stream: io.BufferedReader) -> None:
            # WHY 直接写 dict：CPython 下单次字典赋值是原子的，且两个线程
            # 写的是不同的键，无需再加一把锁。
            slots[name] = _read_bounded(stream, limit)

        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            if stream is None:
                continue
            worker = threading.Thread(
                target=reader,
                args=(name, stream),
                name=f"wsl-{name}-reader",
                daemon=True,
            )
            worker.start()
            workers.append(worker)

        timed_out = False
        try:
            exit_code = process.wait(timeout=request.timeout + _GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # WHY 走到这里说明 Linux 侧的 timeout 没能收尾（发行版卡住、
            # 或命令脱离了进程组），此时只能从 Windows 侧切断。
            logger.warning(
                "WSL 命令超过 Windows 侧兜底时限（%ss+%ss），终止 wsl.exe",
                request.timeout,
                _GRACE_SECONDS,
            )
            process.kill()
            try:
                process.wait(timeout=_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                logger.error("wsl.exe 未能在 %ss 内退出，可能存在残留进程", _GRACE_SECONDS)
            exit_code = _TIMEOUT_EXIT_CODE
            timed_out = True

        # WHY 只等有限时间：若 Linux 侧有进程脱离了进程组并继续持有管道写端，
        # 读线程会一直阻塞；它们是守护线程，不会阻止解释器退出，放弃等待的
        # 代价只是丢掉残余输出，好过整个调用永久挂起。
        for worker in workers:
            worker.join(timeout=_STREAM_JOIN_SECONDS)
            if worker.is_alive():
                logger.warning("输出读线程 %s 仍未结束，已放弃等待残余输出", worker.name)

        out_bytes, out_total = slots.get("stdout", (b"", 0))
        err_bytes, err_total = slots.get("stderr", (b"", 0))

        if exit_code == _TIMEOUT_EXIT_CODE:
            timed_out = True

        return _CapturedOutput(
            stdout=out_bytes,
            stderr=err_bytes,
            stdout_truncated=out_total > limit,
            stderr_truncated=err_total > limit,
            exit_code=exit_code,
            timed_out=timed_out,
        )

    def _discover_distro(self) -> str | None:
        """确定可用的发行版。"""
        if self._requested_distro:
            if self._check_distro(self._requested_distro):
                logger.info("WSL 档位使用指定发行版：%s", self._requested_distro)
                return self._requested_distro
            logger.error(
                "指定的 WSL 发行版 %s 不可用或不满足要求（需要 bash、timeout 与 /mnt）",
                self._requested_distro,
            )
            return None

        for name in self._list_distros():
            if self._check_distro(name):
                logger.info("WSL 档位自动选定发行版：%s", name)
                return name
            logger.warning("WSL 发行版 %s 不满足沙箱要求，跳过", name)
        logger.warning("没有可用的 WSL 发行版，WSL 档位不可用")
        return None

    def _list_distros(self) -> list[str]:
        """列出已安装的发行版名称。"""
        argv = [_WSL_EXE, "--list", "--quiet"]
        try:
            completed = subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                timeout=self._probe_timeout,
                check=False,
            )
        except OSError as exc:
            logger.warning("无法执行 %s（%s），WSL 档位不可用", _WSL_EXE, exc)
            return []
        except subprocess.SubprocessError as exc:
            logger.warning("列举 WSL 发行版失败（%s），WSL 档位不可用", exc)
            return []

        if completed.returncode != 0:
            logger.warning(
                "列举 WSL 发行版失败（退出码 %s）：%s",
                completed.returncode,
                _decode_meta(completed.stderr).strip(),
            )
            return []

        names: list[str] = []
        for line in _decode_meta(completed.stdout).splitlines():
            # WHY 去掉 NUL 与默认发行版标记：部分 Windows 版本上该命令输出
            # UTF-16，且默认发行版会带一个前导 ``*``。
            cleaned = line.replace("\x00", "").strip().lstrip("*").strip()
            if not cleaned or cleaned.lower().startswith(_WSL_NOISE_PREFIX):
                continue
            names.append(cleaned)
        return names

    def _check_distro(self, name: str) -> bool:
        """检查单个发行版是否满足沙箱要求。"""
        script = (
            "command -v bash >/dev/null 2>&1 && "
            "command -v timeout >/dev/null 2>&1 && "
            "test -d /mnt && echo WSL_SANDBOX_PROBE_OK"
        )
        argv = [_WSL_EXE, "--distribution", name, "--exec", "sh", "-c", script]
        try:
            completed = subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                timeout=self._probe_timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("探测 WSL 发行版 %s 失败：%s", name, exc)
            return False

        if completed.returncode != 0:
            logger.debug(
                "WSL 发行版 %s 探测退出码 %s：%s",
                name,
                completed.returncode,
                _decode_meta(completed.stderr).strip(),
            )
            return False
        return _PROBE_OK_TOKEN in completed.stdout


def _to_wsl_path(path: Path) -> str:
    """把 Windows 路径换算成 WSL 内的 ``/mnt/<盘符>/...`` 路径。

    WHY 必须换算：工作区在 Windows 侧，而命令在 Linux 侧运行，直接把
    ``C:\\...`` 交给 ``cd`` 只会得到一个「目录不存在」。
    """
    resolved = Path(path).resolve()
    posix = resolved.as_posix()
    if not resolved.drive:
        # 无盘符：本身就不是 Windows 本地路径，原样交给 Linux 侧判断。
        return posix
    drive = resolved.drive[0].lower()
    return f"/mnt/{drive}{posix[len(resolved.drive) :]}"


def _decode(raw: bytes) -> str:
    """解码命令输出字节。

    WHY 不做 UTF-16 回退：命令自身的输出几乎总是 UTF-8，而 ``wsl.exe`` 的
    诊断在含中文时是 UTF-16LE，两者会混进同一条 stderr。按「是否含 NUL」
    整体猜编码，等于为了读清一行噪声而把命令的真实报错解成乱码——命令输出
    才是主角。统一按 UTF-8 解，UTF-16 噪声会带出 NUL，由 ``_strip_noise``
    整行丢弃。
    """
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def _decode_meta(raw: bytes) -> str:
    """解码 ``wsl.exe`` 的元信息输出（发行版列表等）。

    WHY 与命令输出分开处理：整条流都是 wsl.exe 自己的输出，不存在混合编码
    问题，此时按 NUL 比例猜 UTF-16 是安全的。
    """
    if not raw:
        return ""
    if raw.count(b"\x00") * 4 > len(raw):
        try:
            return raw.decode("utf-16-le").replace("\x00", "")
        except UnicodeDecodeError:
            logger.debug("UTF-16 解码失败，回退 UTF-8 解码")
    return raw.decode("utf-8", errors="replace")


def _strip_noise(text: str) -> str:
    """剔除 stderr 里由 ``wsl.exe`` 与 bash 产生的、与命令无关的噪声。

    WHY 必须过滤：``wsl.exe`` 会把「检测到 localhost 代理配置」这类诊断写进
    stderr，bash 也会为被终止的作业打印回执；它们与命令的真实错误混排之后，
    模型会把运行环境的问题当成命令失败去排查，越查越偏。
    """
    kept = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        # WHY 整行丢弃含 NUL 的行：那是 UTF-16 的 wsl.exe 诊断混进了 UTF-8
        # 流后的残留，逐字符抢救只会得到一堆替换符。
        if "\x00" in line:
            continue
        if stripped.lower().startswith(_WSL_NOISE_PREFIX):
            continue
        if _JOB_NOTICE_PATTERN.match(stripped):
            continue
        kept.append(line)
    return "\n".join(kept)


def _read_bounded(stream: io.BufferedReader, limit: int) -> tuple[bytes, int]:
    """读取流，最多保留 ``limit`` 字节，其余丢弃但计入总数。

    WHY 仍要统计被丢弃的字节数：截断标记必须由「真实长度 > 阈值」判定，
    只读前 N 字节会把「刚好输出 N 字节」误报成截断。

    Returns:
        ``(保留的字节, 读到的总字节数)``。
    """
    chunks: list[bytes] = []
    kept = 0
    total = 0
    try:
        while True:
            chunk = stream.read1(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if kept < limit:
                piece = chunk[: limit - kept]
                chunks.append(piece)
                kept += len(piece)
    except (OSError, ValueError) as exc:
        # WHY 吞掉读异常但记日志：进程被杀时管道会破裂（ERROR_BROKEN_PIPE），
        # 这属于预期的收尾流程，已读到的输出依然有效。
        logger.debug("读取子进程输出中断：%s", exc)
    finally:
        try:
            stream.close()
        except OSError as exc:
            logger.debug("关闭子进程输出流失败，忽略：%s", exc)
    return b"".join(chunks), total
