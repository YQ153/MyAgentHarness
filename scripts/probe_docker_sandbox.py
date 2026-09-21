"""T24 开工前探针：Docker 沙箱（Tier 2）在本机到底能做到哪一步。

计划要求「先定三件事再写代码」——宿主需求、威胁模型、网络策略。这三件事都不能靠
推断：容器能不能起、工作区能不能挂、断网是不是真的断了、随便怎么杀会不会留残留，
都是**本机实测**才成立的事实，而它们直接决定实现形态与配置项的默认值。

只读性：本探针不修改仓库，所有落盘都在 ``.data/docker-probe/``（已在 .gitignore 内），
容器用 ``--rm`` 起、用完即删。

用法::

    python scripts/probe_docker_sandbox.py
    python scripts/probe_docker_sandbox.py --image python:3.14-slim

退出码：0 全部通过；1 有项目失败；2 宿主不可用（无 Docker 或非 Linux 容器模式）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = ROOT / ".data" / "docker-probe"
"""探针工作目录（.gitignore 覆盖范围内），也是被挂载进容器的那个目录。"""

DEFAULT_IMAGE = "python:3.14-slim"
"""候选执行镜像。

WHY 选它：本机已有该镜像（T22 期间拉取），因此基础镜像不必联网即可验证；且它同时
带 Python 与 shell，能覆盖「Agent 在容器里跑脚本」这一主要用途。
"""

_MARKER = "DOCKER_SANDBOX_MARKER_OK"


class _Result:
    """一条探针结论。"""

    def __init__(self, name: str, ok: bool, detail: str) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail


def _docker(args: list[str], *, timeout: float = 60.0) -> tuple[int, str, str]:
    """调用 docker CLI。

    WHY 所有调用都带超时：不可达的 registry 会让 ``docker pull`` 挂上几分钟；探针若
    卡住就失去了「先取得事实」的意义。超时按失败处理（连超时的时间也一并记录，
    那本身就是结论的一部分）。
    """
    try:
        completed = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"超时（>{timeout}s）"
    except FileNotFoundError:
        return 127, "", "找不到 docker 可执行文件"
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _image_exists(image: str) -> tuple[bool, str]:
    """本机是否已有该镜像（有则整条链路不依赖网络）。

    Returns:
        ``(是否存在, 判定细节)``。

    WHY 不用 ``docker image inspect <ref>`` 判定：本机（Docker Desktop 4.41 /
    Engine 28.1.1 / overlayfs 存储驱动）实测该命令对**真实存在且能正常运行**的
    ``python:3.14-slim`` 返回 ``No such image``，而 ``docker image ls --filter``
    与 ``docker run --pull=never`` 都正常。拿它当判据会把「镜像就绪」误判成
    「需联网拉取」，进而把一个本来能跑的档位判死——这与「档位语义不放宽」是两回事：
    前者是能力探测出错，后者是有意报错。
    """
    code, out, err = _docker(
        ["image", "ls", "--filter", f"reference={image}", "--format", "{{.ID}}"], timeout=30.0
    )
    if code != 0:
        return False, err or out or "docker image ls 失败"
    image_id = out.splitlines()[0] if out.strip() else ""
    return bool(image_id), (f"{image}（ID {image_id[:12]}）" if image_id else f"{image} 不在本机")


# --------------------------------------------------------------- 分段


def probe_host() -> list[_Result]:
    """第 1 段：宿主需求（版本、容器模式、内存）。"""
    print(f"=== 第 1 段：宿主（Python {sys.version_info.major}.{sys.version_info.minor}） ===")
    results: list[_Result] = []

    code, out, err = _docker(["info", "--format", "{{json .}}"], timeout=30.0)
    if code != 0:
        results.append(_Result("docker daemon 可用", False, err or out or "docker info 失败"))
        return results

    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        results.append(_Result("docker info 可解析", False, out[:200]))
        return results

    server = str(info.get("ServerVersion", ""))
    results.append(_Result("docker daemon 可用", bool(server), f"ServerVersion={server}"))

    ostype = str(info.get("OSType", ""))
    # WHY 必须判它：Windows 容器模式下 ``python:3.14-slim``（Linux 镜像）根本起不来，
    # 而报错文本是「no matching manifest」，与「镜像不存在」长得一样。
    results.append(
        _Result("容器模式为 linux", ostype == "linux", f"OSType={ostype!r}（Docker Desktop 的 WSL2 后端）")
    )

    memory = info.get("MemTotal")
    if isinstance(memory, int):
        results.append(_Result("可用内存", memory > 2 * 1024**3, f"{memory / 1024**3:.1f} GiB"))
    else:
        results.append(_Result("可用内存", True, "未上报（跳过）"))

    for item in results:
        print(f"  [{'OK' if item.ok else '!!'}] {item.name}：{item.detail}")
    return results


def probe_image(image: str) -> list[_Result]:
    """第 2 段：基础镜像可得性。"""
    print("\n=== 第 2 段：执行镜像 ===")
    local, detail = _image_exists(image)
    if local:
        detail = f"{detail}：本机已有，整条链路无需联网"
    else:
        detail = f"{detail}：需联网拉取（Docker Hub 在部分网络下不可达）"
    print(f"  [{'OK' if local else '!!'}] 执行镜像就绪 —— {detail}")
    return [_Result("执行镜像就绪", local, detail)]


def probe_mount(image: str) -> list[_Result]:
    """第 3 段：工作区挂载与读写形态。

    WHY 这一段最关键：挂载决定了 Agent 在容器里能不能改文件，也决定了「容器内写的
    文件回到宿主是什么属主」。T24 的挂载范围必须**仅限** workspace，而 Windows 宿主
    只能绑定挂载（bind mount），其权限行为与命名卷不同。
    """
    print("\n=== 第 3 段：挂载 ===")
    results: list[_Result] = []

    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    (PROBE_DIR / "host.txt").write_text("written-by-host\n", encoding="utf-8")

    mount = f"{PROBE_DIR}:/work"
    script = (
        "import pathlib,os;"
        "p=pathlib.Path('/work');"
        "print('READ', (p/'host.txt').read_text().strip());"
        "(p/'container.txt').write_text('written-by-container\\n');"
        "st=(p/'container.txt').stat();"
        "print('UID', st.st_uid, 'MODE', oct(st.st_mode)[-3:]);"
        "print('CWD', os.path.realpath('/work'))"
    )
    code, out, err = _docker(
        ["run", "--rm", "--network", "none", "-v", mount, image, "python", "-c", script],
        timeout=120.0,
    )

    read_ok = "READ written-by-host" in out
    results.append(_Result("容器能读到宿主写入的文件", read_ok, out or err))

    write_ok = (PROBE_DIR / "container.txt").is_file()
    results.append(
        _Result(
            "容器写入能回到宿主（读写挂载可行）",
            write_ok,
            (PROBE_DIR / "container.txt").read_text(encoding="utf-8").strip() if write_ok else err,
        )
    )

    # 只读挂载：若可用，将来可给 Agent 一个「只读工作区 + 可写临时区」的形态。
    code, out, err = _docker(
        [
            "run", "--rm", "--network", "none",
            "-v", f"{PROBE_DIR}:/work:ro",
            image, "sh", "-c", "echo ro-probe > /work/should-fail || echo RO_BLOCKED",
        ],
        timeout=120.0,
    )
    # WHY 单独取出这一行：只读挂载被拒绝时 stderr 也会有内容，但「有没有被拒绝」只看
    # 我们自己的标记，不看退出码——sh -c 的短路写法会让退出码仍为 0。
    results.append(_Result("只读挂载确实拦截写入", "RO_BLOCKED" in out, out or err))

    for item in results:
        print(f"  [{'OK' if item.ok else '!!'}] {item.name}：{item.detail}")
    return results


def probe_network(image: str) -> list[_Result]:
    """第 4 段：``--network none`` 是不是真的断网。"""
    print("\n=== 第 4 段：网络策略 ===")
    script = (
        "import socket;"
        "\nprint('START');"
        "\ntry:\n    socket.gethostbyname('example.com'); print('DNS_OK')\n"
        "except Exception as exc:\n    print('DNS_BLOCKED', type(exc).__name__)"
    )
    code, out, err = _docker(
        ["run", "--rm", "--network", "none", image, "python", "-c", script], timeout=120.0
    )
    ok = "DNS_BLOCKED" in out
    detail = "容器内无法解析域名" if ok else (out or err)
    print(f"  [{'OK' if ok else '!!'}] --network none 断网：{detail}")
    return [_Result("--network none 生效", ok, detail)]


def probe_lifecycle(image: str) -> list[_Result]:
    """第 5 段：超时中止与残留清理。

    WHY 关心 kill 的**耗时**：中止语义要求「用户点停止后尽快真的停下」。若 ``docker
    kill`` 要等几十秒，就无法在中止路径上同步等待它——那时需要先返回、再异步收尾。
    """
    print("\n=== 第 5 段：生命周期 ===")
    results: list[_Result] = []

    name = "harness-docker-probe-lifecycle"
    _docker(["rm", "-f", name], timeout=30.0)  # 清掉可能的同名残留

    code, out, err = _docker(
        ["run", "-d", "--rm", "--name", name, "--network", "none", image, "sleep", "120"],
        timeout=120.0,
    )
    started = code == 0
    results.append(_Result("能以分离模式起容器", started, out or err))

    if started:
        time.sleep(1.0)
        begin = time.perf_counter()
        code, out, err = _docker(["kill", name], timeout=60.0)
        elapsed = time.perf_counter() - begin
        # WHY 阈值取 10 s：中止路径上若超过这个量级就必须异步化，而「异步收尾」会
        # 引入一份新的状态（谁负责最后 rm），值得在定稿时就知道。
        results.append(
            _Result("docker kill 及时返回", code == 0 and elapsed < 10.0, f"{elapsed:.2f} s")
        )

        # ``--rm`` 下容器被 kill 后应由 docker 自身移除；这里确认「宿主无残留」。
        time.sleep(1.0)
        code, out, _ = _docker(["ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"])
        results.append(_Result("中止后宿主无残留容器", name not in out, out or "（无）"))

    for item in results:
        print(f"  [{'OK' if item.ok else '!!'}] {item.name}：{item.detail}")
    return results


def probe_timing(image: str) -> list[_Result]:
    """第 6 段：单次执行的开销。

    WHY 必须量化：容器冷启动（拉镜像之外的部分）决定了「每条命令起一个容器」是否可行。
    若单次开销是秒级，就当不成 ``execute`` 的实现；若在百毫秒级，才是可用的形态。
    """
    print("\n=== 第 6 段：单次执行开销 ===")
    _docker(["run", "--rm", "--network", "none", image, "true"], timeout=120.0)  # 预热

    samples: list[float] = []
    for _ in range(3):
        begin = time.perf_counter()
        code, _, err = _docker(
            ["run", "--rm", "--network", "none", "-v", f"{PROBE_DIR}:/work", image,
             "sh", "-c", f"echo {_MARKER}"],
            timeout=120.0,
        )
        samples.append(time.perf_counter() - begin)
        if code != 0:
            print(f"  [!!] 执行失败：{err}")
            return [_Result("单次执行", False, err)]

    best = min(samples)
    # WHY 用最小值：这里要测的是「框架开销」，首次运行混入了页缓存未命中等一次性因素，
    # 取最小值比取平均更接近稳态。
    detail = f"最快 {best:.2f} s（3 次：{[round(s, 2) for s in samples]}）"
    ok = best < 5.0
    print(f"  [{'OK' if ok else '!!'}] 单次 run 往返：{detail}")
    return [_Result("单次 run 往返", ok, detail)]


def main() -> int:
    """跑完全部分段并给出结论。"""
    parser = argparse.ArgumentParser(description="Docker 沙箱开工前探针")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"执行镜像（默认 {DEFAULT_IMAGE}）")
    args = parser.parse_args()

    if shutil.which("docker") is None:
        print("找不到 docker 可执行文件：本机不具备 Tier 2 的宿主条件。")
        return 2

    host = probe_host()
    if not host or not all(item.ok for item in host):
        print("\n宿主条件不满足，后续分段无法进行。")
        return 2

    results: list[_Result] = []
    results += probe_image(args.image)
    if not results[-1].ok:
        print("\n执行镜像不可用，后续分段无法进行。")
        return 2

    results += probe_mount(args.image)
    results += probe_network(args.image)
    results += probe_lifecycle(args.image)
    results += probe_timing(args.image)

    failed = [item for item in results if not item.ok]
    print("\n=== 结论 ===")
    print(f"  {len(results) - len(failed)}/{len(results)} 项通过")
    for item in failed:
        print(f"  未通过：{item.name} —— {item.detail}")
    print(f"\n探针目录：{PROBE_DIR}（可整个删除）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
