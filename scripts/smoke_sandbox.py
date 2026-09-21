"""沙箱档位冒烟验证：Tier 0（进程沙箱）与 Tier 1（WSL 发行版沙箱）。

两类验证刻意分成两段而不是复用同一个 runner：两档位的 shell 方言不同
（Windows 用 ``set``/``for /l``，Linux 用 ``env``/``seq``），混在一起只会
得到一堆「命令不存在」的假失败。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
# WHY 强制 UTF-8 输出：Windows 控制台默认 GBK，WSL 侧的错误信息里含非 GBK
# 字符时，print 会直接抛 UnicodeEncodeError，把验证结果变成一条假失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["EXECUTION_MODE"] = "sandbox"
os.environ["SANDBOX_TIER"] = "auto"
os.environ["SANDBOX_TIMEOUT"] = "10"
os.environ["SANDBOX_MAX_OUTPUT_BYTES"] = "500"
os.environ["DEEPSEEK_API_KEY"] = "sk-should-not-leak"
os.environ["HTTP_PROXY"] = "http://should-not-leak:8080"

import logging  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from deepagents.backends.protocol import SandboxBackendProtocol  # noqa: E402

from agent.guardrails import build_interrupt_on  # noqa: E402
from agent.sandbox_backend import SandboxedFilesystemBackend  # noqa: E402
from config import AppConfig  # noqa: E402
from runtime.sandbox import CommandRequest, SandboxPolicy, build_sandbox_runner  # noqa: E402
from runtime.sandbox.errors import SandboxUnavailableError  # noqa: E402
from runtime.sandbox.process_runner import ProcessSandboxRunner  # noqa: E402
from runtime.sandbox.wsl_runner import WslSandboxRunner  # noqa: E402

config = AppConfig.load()

def _session_root(config: AppConfig, name: str = "smoke") -> pathlib.Path:
    """取一个冒烟用的会话根（并建出来）。

    WHY 不再读某个「启动默认工作区」：新模型下每个会话的根由它自己决定——用户选的工作
    空间，或应用为它建的专属目录。冒烟脚本是一段「手工会话」，因此显式取一个专属目录。
    """
    root = config.session_dir(name)
    root.mkdir(parents=True, exist_ok=True)
    return root

print(f"[cfg] mode={config.execution_mode} tier={config.sandbox_tier} ws={workspace}")

auto_runner = build_sandbox_runner(config)
print(f"[0 auto 选档] tier={auto_runner.tier.value} desc={auto_runner.describe()}")

policy = SandboxPolicy.from_config(config)

# ==================== Tier 0：进程沙箱 ====================
print("\n--- Tier 0：进程沙箱 ---")
runner = ProcessSandboxRunner(policy)
print(f"[probe] available={runner.probe()} desc={runner.describe()}")

r1 = runner.run(CommandRequest(command="echo hello-sandbox", cwd=workspace, timeout=10))
print(f"[1 基本执行] stdout={r1.stdout!r} exit={r1.exit_code} timed_out={r1.timed_out}")

r2 = runner.run(CommandRequest(command="set", cwd=workspace, timeout=10))
env_lines = [line for line in r2.stdout.splitlines() if "=" in line]
env_names = sorted(line.split("=", 1)[0].upper() for line in env_lines)
leaked = [line for line in env_lines if "should-not-leak" in line.lower()]
# WHY 改用 ``set`` 枚举而非 ``echo %VAR%``：cmd 对「未定义的变量」会原样输出
# ``%VAR%``，看输出像是泄漏了，实则该变量根本不存在——这个断言会骗人。
print(f"[2 环境清洗] 可见变量数={len(env_names)} 泄漏项={leaked} 期望泄漏项=[]")
print(f"            变量清单={env_names}")
print(f"            PATH 已透传={any(n == 'PATH' for n in env_names)} 期望 True")

r3 = runner.run(
    CommandRequest(command="ping -n 20 127.0.0.1 >nul", cwd=workspace, timeout=3)
)
print(f"[3 超时终止] timed_out={r3.timed_out} exit={r3.exit_code} 期望 True/124")

r4 = runner.run(
    CommandRequest(
        command="for /l %i in (1,1,200) do @echo 0123456789abcdefghij",
        cwd=workspace,
        timeout=10,
        max_output_bytes=500,
    )
)
print(f"[4 输出截断] truncated={r4.truncated} len={len(r4.stdout)} 期望 True/500")

backend = SandboxedFilesystemBackend(
    root_dir=str(workspace),
    runner=runner,
    timeout=config.sandbox_timeout,
    max_output_bytes=config.sandbox_max_output_bytes,
    inherit_env=False,
)
print(f"[5 协议判定] is_sandbox_backend={isinstance(backend, SandboxBackendProtocol)} id={backend.id}")

resp = backend.execute("echo via-backend")
print(f"[6 backend 执行] output={resp.output!r} exit={resp.exit_code}")

resp2 = backend.execute("echo boom 1>&2 & exit 3")
print(f"[7 stderr/退出码] output={resp2.output!r} exit={resp2.exit_code}")

resp3 = backend.execute("")
print(f"[8 空命令] output={resp3.output!r} exit={resp3.exit_code}")

print(f"[9 护栏] {build_interrupt_on(config.execution_mode, config.sandbox_tier)}")
print(f"[10 护栏-关闭审批] {build_interrupt_on(config.execution_mode, config.sandbox_tier, require_approval=False)}")

# 进程数上限是本档位唯一能挡住 fork bomb 的手段，用一个刻意压低的上限验证：
# 上限 4 时派生 20 个后台进程必然被 Job Object 拒绝，宿主不应出现进程风暴。
tight_runner = ProcessSandboxRunner(
    SandboxPolicy(
        max_processes=4,
        max_memory_mb=config.sandbox_max_memory_mb,
        cpu_percent=config.sandbox_cpu_percent,
    )
)
r11 = tight_runner.run(
    CommandRequest(
        command="for /l %i in (1,1,20) do @start /b cmd /c ping -n 3 127.0.0.1 >nul",
        cwd=workspace,
        timeout=20,
    )
)
print(
    f"[11 进程数上限] exit={r11.exit_code} stderr非空={bool(r11.stderr.strip())} "
    f"stdout片段={r11.stdout.strip()[:120]!r}"
)
print("              期望：有报错痕迹（stderr 非空或 exit!=0），且宿主未被拖垮")

# ==================== Tier 1：WSL 发行版沙箱 ====================
print("\n--- Tier 1：WSL 发行版沙箱 ---")
wsl_runner = WslSandboxRunner(policy, distro=config.sandbox_wsl_distro)
wsl_available = wsl_runner.probe()
print(f"[12 WSL 探测] available={wsl_available} distro={wsl_runner.distro}")
print(f"              desc={wsl_runner.describe()}")

if not wsl_available:
    print("              WSL 不可用，跳过 Tier 1 其余验证项（auto 应已回落到 process）")
else:
    w1 = wsl_runner.run(
        CommandRequest(command="echo hello-wsl; uname -s", cwd=workspace, timeout=30)
    )
    print(f"[13 基本执行] stdout={w1.stdout.strip()!r} exit={w1.exit_code} 期望含 Linux")

    w2 = wsl_runner.run(CommandRequest(command="pwd", cwd=workspace, timeout=30))
    print(f"[14 工作目录换算] pwd={w2.stdout.strip()!r} 期望以 /mnt/ 开头")
    print(f"                 工作区={workspace}")

    w3 = wsl_runner.run(CommandRequest(command="env | sort", cwd=workspace, timeout=30))
    wsl_env_leaked = [
        line
        for line in w3.stdout.splitlines()
        if "should-not-leak" in line.lower() or "deepseek" in line.lower()
    ]
    wsl_env_names = sorted(
        line.split("=", 1)[0] for line in w3.stdout.splitlines() if "=" in line
    )
    print(f"[15 环境清洗] 泄漏项={wsl_env_leaked} 期望 []")
    print(f"              变量清单={wsl_env_names}")

    w4 = wsl_runner.run(CommandRequest(command="sleep 30", cwd=workspace, timeout=3))
    print(f"[16 超时终止] timed_out={w4.timed_out} exit={w4.exit_code} 期望 True/124 或 137")

    w5 = wsl_runner.run(
        CommandRequest(
            command="for i in $(seq 1 300); do echo 0123456789abcdefghij; done",
            cwd=workspace,
            timeout=30,
            max_output_bytes=500,
        )
    )
    print(f"[17 输出截断] truncated={w5.truncated} len={len(w5.stdout)} 期望 True/500")

    tight_wsl = WslSandboxRunner(
        SandboxPolicy(
            max_processes=4,
            max_memory_mb=config.sandbox_max_memory_mb,
            cpu_percent=config.sandbox_cpu_percent,
        ),
        distro=config.sandbox_wsl_distro,
    )
    w6 = tight_wsl.run(
        CommandRequest(
            command="for i in $(seq 1 30); do (sleep 5 &); done; sleep 1; echo done",
            cwd=workspace,
            timeout=30,
        )
    )
    print(
        f"[18 进程数上限] exit={w6.exit_code} stderr非空={bool(w6.stderr.strip())} "
        f"stderr片段={w6.stderr.strip()[:100]!r}"
    )
    print("               期望：fork 被拒（stderr 有报错或 exit!=0）")

    # WHY 验证写入：cwd 换算对不对，最有说服力的证据不是 pwd 的输出，而是
    # 命令在「它以为的当前目录」里写下的文件，确实出现在宿主的 workspace 里。
    probe_file = workspace / "_wsl_probe.txt"
    try:
        wsl_runner.run(
            CommandRequest(
                command="echo written-by-wsl > _wsl_probe.txt",
                cwd=workspace,
                timeout=30,
            )
        )
        content = probe_file.read_text(encoding="utf-8").strip() if probe_file.is_file() else ""
        print(f"[19 写入工作区] 宿主可见={probe_file.is_file()} 内容={content!r}")
    finally:
        probe_file.unlink(missing_ok=True)

    wsl_backend = SandboxedFilesystemBackend(
        root_dir=str(workspace),
        runner=wsl_runner,
        timeout=config.sandbox_timeout,
        max_output_bytes=config.sandbox_max_output_bytes,
        inherit_env=False,
    )
    wsl_resp = wsl_backend.execute("echo via-wsl-backend")
    print(f"[20 backend 集成] id={wsl_backend.id} 期望前缀 sandbox-wsl-")
    print(f"                  output={wsl_resp.output!r} exit={wsl_resp.exit_code}")

# ==================== 档位装配路径 ====================
print("\n--- 档位装配 ---")
os.environ["SANDBOX_TIER"] = "wsl"
explicit_runner = build_sandbox_runner(AppConfig.load())
print(f"[21 显式 wsl 档位] tier={explicit_runner.tier.value} 期望 wsl")

os.environ["SANDBOX_TIER"] = "docker"
# WHY 用「一个不存在的镜像」而不是「一个没实现的档位」：四个档位都已实现，「未实现」
# 这条分支不再可达；但当时要钉的性质——**档位不可用时报错，而不是就近降级到更弱的
# 隔离**——仍然有效，只是现在只能靠探测失败来触发（T24 把它从「未实现」升级成了
# 「已实现但探测失败时报错」）。
os.environ["SANDBOX_DOCKER_IMAGE"] = "harness-nonexistent:latest"
try:
    build_sandbox_runner(AppConfig.load())
    print("[22 档位不可用时不得降级] 期望抛错但成功返回，断言失败")
except SandboxUnavailableError as exc:
    print(f"[22 档位不可用时不得降级] 正确报错：{str(exc)[:90]}")
