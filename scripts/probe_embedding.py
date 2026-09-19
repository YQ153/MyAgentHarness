"""嵌入后端的开工前探针：决定 T22 走哪条路，并把参数从「拍脑袋」变成实测量。

四段，逐段可独立结论：

1. **轮子可用性**（默认执行，只需网络）：查 PyPI 元数据，判断候选包在**当前解释器
   版本**（本机为 CPython 3.14）上有没有可安装的轮子。这一段决定 `in-process` 与
   `subprocess` 两条路是否成立——装不上就只剩容器 / 托管路线。
2. **容器可达性**（默认执行，只需 Docker）：`docker manifest inspect` 只查注册表元数据、
   **不下载任何层**，用它确认候选镜像存在且本机拉得动。比 `docker pull` 快几个数量级，
   也避免把几百 MB 层留在磁盘上。
3. **实测量**（`--measure` 才执行）：在独立 venv 里装 `fastembed`，以 `--child` 模式把
   本文件拉起来当嵌入服务，量冷启动耗时、常驻内存、批量吞吐与 stdio 往返开销。
4. **协议原型**（`--child`）：行分隔 JSON + base64(float32) 的 stdio 服务。它就是
   `scripts/embed_server.py` 的原型，因此测量跑的是**真实协议**而不是另写的基准。

WHY 量的是「进程 + stdio」而不是「直接 import」：生产形态是子进程承载模型，主进程
不 import 重依赖；直接 import 测出来的冷启动与内存不能代表那条路径。

用法：
    python scripts/probe_embedding.py              # 只做第 1、2 段
    python scripts/probe_embedding.py --measure    # 追加第 3 段（会建 venv 并下模型）

退出码：``0`` 全部有结论 / ``2`` 有跳过 / ``1`` 有失败。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import pathlib
import struct
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
# WHY 强制 UTF-8 输出：Windows 控制台默认 GBK，轮子文件名里的非 ASCII 或模型回显都会
# 触发 UnicodeEncodeError，把一次成功测量的结论变成假失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROBE_DIR = ROOT / ".data"
"""探针工作目录（在 .gitignore 覆盖范围内）。

WHY 与生产共用 ``.data``：下面的 ``venv`` 子目录名与 ``scripts/setup_embed_venv.py``
建出的完全一致，于是探针**测的就是应用实际会用的那个环境**，同时不必为验证多装一份
onnxruntime（数百 MB）。「测的」与「跑的」不是同一个环境，是这类探针最容易失去意义的
地方。
"""

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
"""测量用模型：中文可用、体积小（约 95 MB）、维度 512——先用手头能拿到的最轻中文模型
把「这条路能不能走」问清楚，再谈换更大的模型。"""

CANDIDATE_PACKAGES = (
    # 走 onnxruntime 的轻量路线：主进程与子进程都只需 numpy 级依赖
    "fastembed",
    "onnxruntime",
    "model2vec",
    # 走 torch 的重路线：质量上限高，但体积与轮子风险都最大
    "sentence-transformers",
    "torch",
    "transformers",
    # T22 其余环节的候选
    "langchain-text-splitters",
    "sqlite-vec",
)

CANDIDATE_IMAGES = (
    # 容器内跑嵌入服务的两个现实选项
    ("ghcr.io/huggingface/text-embeddings-inference", "cpu-1.8"),
    ("ollama/ollama", "latest"),
    # 顺带复核 T14 当时被 Docker 卡住的那条路是否已通
    ("searxng/searxng", "latest"),
)


# ------------------------------------------------------------------ 第 1 段：轮子


def _wheel_tags(filename: str) -> dict[str, str] | None:
    """拆出 wheel 文件名的 python / abi / platform 三段标签。"""
    if not filename.endswith(".whl"):
        return None
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        return None
    return {"python": parts[-3], "abi": parts[-2], "platform": parts[-1]}


def _platform_matches(platform_tag: str) -> bool:
    """该 wheel 的平台标签是否匹配本机。"""
    if platform_tag == "any":
        return True
    if sys.platform == "win32":
        return platform_tag.startswith("win")
    if sys.platform.startswith("linux"):
        return "manylinux" in platform_tag or platform_tag.startswith("linux")
    if sys.platform == "darwin":
        return platform_tag.startswith("macosx")
    return False


def _installability(tags: dict[str, str], cp_tag: str) -> str:
    """判断某个 wheel 能否装在本机解释器上。

    Returns:
        ``installable`` / ``freethreaded``（只有免 GIL 构建的轮子）/ ``no``。

    WHY 不能只看 python 标签里有没有 ``cp314``：标签形如 ``py3-none-win_amd64``
    的轮子虽然带平台后缀，但 **abi 为 none 表示它不依赖 CPython 的 ABI**——这类轮子
    （``sqlite-vec`` 就是）在 3.14 上照样能装。把 ``none`` 当成「平台不匹配」会让探针
    给出假阴性，据此放弃一条本来可行的路线。

    WHY 单独识别 ``cp314t``：``t`` 是免 GIL（free-threaded）构建的 ABI。本机是常规
    构建，装了也 import 不了——子串匹配会把这种情况误判成可用。
    """
    if not _platform_matches(tags["platform"]):
        return "no"
    abi = tags["abi"]
    if abi == "none" or tags["python"].split(".")[0] == "py3":
        return "installable"
    if abi == cp_tag:
        return "installable"
    if abi.endswith("t") and abi[: -len("t")] == cp_tag:
        return "freethreaded"
    return "no"


def probe_wheels(client: object) -> list[dict[str, object]]:
    """查每个候选包最新版是否有可在本机安装的轮子。"""
    cp_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    reports: list[dict[str, object]] = []

    for name in CANDIDATE_PACKAGES:
        report: dict[str, object] = {"name": name, "version": "", "ok": False, "note": ""}
        try:
            payload = client.get(f"https://pypi.org/pypi/{name}/json").json()
        except Exception as exc:  # 网络不可达 / 包名写错 / 响应不是 JSON
            report["note"] = f"查询失败：{type(exc).__name__}: {exc}"
            reports.append(report)
            continue

        version = str(payload.get("info", {}).get("version", ""))
        report["version"] = version
        requires = str(payload.get("info", {}).get("requires_python", "") or "")
        report["note"] = f"requires_python={requires or '未声明'}"

        files = [item["filename"] for item in payload.get("releases", {}).get(version, [])]
        installable: list[str] = []
        freethreaded: list[str] = []
        for filename in files:
            tags = _wheel_tags(filename)
            if tags is None:
                continue
            kind = _installability(tags, cp_tag)
            if kind == "installable":
                installable.append(filename)
            elif kind == "freethreaded":
                freethreaded.append(filename)

        report["ok"] = bool(installable)
        if installable:
            # 给出一个具体文件名，便于人工复核判定没有误报
            report["note"] = f"{report['note']}；可用轮子示例：{installable[0]}"
        elif freethreaded:
            report["note"] = (
                f"{report['note']}；只有免 GIL（free-threaded）轮子，本机常规构建装不了："
                f"{freethreaded[0]}"
            )
        else:
            report["note"] = f"{report['note']}；无适用于 {sys.platform} 的可用轮子"
        reports.append(report)

    return reports


# ------------------------------------------------------------------ 第 2 段：容器


def _run(args: list[str], timeout: float = 60.0) -> tuple[int, str]:
    """执行外部命令并合并输出，超时按失败处理（返回码 -1）。"""
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        return -1, f"命令不存在：{args[0]}"
    except subprocess.TimeoutExpired:
        return -1, f"超时（{timeout}s）：{' '.join(args)}"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def probe_docker() -> list[dict[str, object]]:
    """确认 Docker 可用，并逐个查候选镜像的 manifest（不下载层）。"""
    reports: list[dict[str, object]] = []

    version_code, version_out = _run(["docker", "version", "--format", "{{.Server.Version}}"], 30.0)
    if version_code != 0:
        return [
            {
                "image": "(docker daemon)",
                "ok": False,
                "note": f"Docker 不可用：{version_out.strip()[:200]}",
            }
        ]

    reports.append({"image": "(docker daemon)", "ok": True, "note": f"Server {version_out.strip()}"})
    for repository, tag in CANDIDATE_IMAGES:
        reference = f"{repository}:{tag}"
        code, out = _run(["docker", "manifest", "inspect", reference], 60.0)
        reports.append(
            {
                "image": reference,
                "ok": code == 0,
                "note": "manifest 可解析（镜像存在且注册表可达）" if code == 0 else out.strip()[:200],
            }
        )
    return reports


# ------------------------------------------------------------------ 第 4 段：子进程协议


def _rss_mb() -> float | None:
    """当前进程的常驻内存（MB）；取不到时返回 ``None`` 而不是猜一个数。"""
    try:
        import resource  # POSIX

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024
    except ImportError:
        pass

    if sys.platform == "win32":
        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process = kernel32.GetCurrentProcess()
        # WHY 两个入口都试：K32GetProcessMemoryInfo 在新系统上由 kernel32 导出，
        # psapi 的旧名在部分环境下才在。任一个成功即可，都失败就如实返回 None。
        for dll_name, func_name in (
            ("kernel32", "K32GetProcessMemoryInfo"),
            ("psapi", "GetProcessMemoryInfo"),
        ):
            try:
                func = getattr(ctypes.WinDLL(dll_name, use_last_error=True), func_name)
            except (OSError, AttributeError):
                continue
            func.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Counters), ctypes.c_ulong]
            func.restype = ctypes.c_int
            if func(process, ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize / (1024 * 1024)
    return None


def _encode(vectors: list[list[float]]) -> tuple[str, int]:
    """把向量编成 base64(float32)，返回 (载荷, 维度)。

    WHY 不用 JSON 数字数组：1536 维 × 1000 块的量级下，数字数组是几十 MB 文本、
    base64 只要约 8 MB。这个差别直接决定千级索引是几秒还是几分钟。
    """
    if not vectors:
        return "", 0
    dims = len(vectors[0])
    flat = [value for vector in vectors for value in vector]
    return base64.b64encode(struct.pack(f"<{len(flat)}f", *flat)).decode("ascii"), dims


def _decode(payload: str, dims: int, count: int) -> list[list[float]]:
    """base64(float32) 还原成向量列表。"""
    raw = struct.unpack(f"<{dims * count}f", base64.b64decode(payload))
    return [list(raw[index * dims : (index + 1) * dims]) for index in range(count)]


def child_loop(model_name: str) -> int:
    """``--child`` 模式：stdio 行分隔 JSON 的嵌入服务。

    WHY 不 import 任何项目模块：该进程跑在**独立 venv** 里（只有模型运行时），
    导入项目代码会立刻 ImportError。这也是将来 ``embed_server.py`` 的硬约束。
    """
    # WHY 必须显式重配 UTF-8：Windows 上子进程的 stdin/stdout 默认跟随 **ANSI 代码页**
    # （本机为 GBK），而父进程按 UTF-8 写入——中文会被按 GBK 解码成乱码并产生孤立代理项，
    # tokenizer 随即拒绝。这类缺陷最阴的地方是**纯 ASCII 时完全正常**：探针第一轮里的
    # 32 条英文测试全过，换成中文才炸。真实场景下若某条路径不报错而是接受了乱码，
    # 就会把「语义完全错误的向量」静默写进索引。故此处以子进程自我重配为准
    # （不依赖父进程设 PYTHONIOENCODING），保证协议编码只有一处真相。
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from fastembed import TextEmbedding  # 只在这个分支导入，父进程不需要它

    started = time.perf_counter()
    model = TextEmbedding(model_name=model_name)
    # 预热一次：onnxruntime 的首次推理含图优化与内存分配，不预热会把它的耗时
    # 算进「冷启动」，让懒启动的阈值定得过高。
    list(model.embed(["warmup"]))
    load_seconds = time.perf_counter() - started

    def respond(payload: dict[str, object]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    respond(
        {
            "id": None,
            "event": "ready",
            "model": model_name,
            "load_seconds": round(load_seconds, 3),
            "rss_mb": _rss_mb(),
        }
    )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            respond({"id": None, "error": f"非法 JSON：{exc}"})
            continue

        if request.get("op") == "shutdown":
            respond({"id": request.get("id"), "ok": True})
            return 0

        texts = request.get("texts") or []
        begin = time.perf_counter()
        try:
            vectors = [list(map(float, item)) for item in model.embed(texts)]
        except Exception as exc:
            # WHY 带上输入形态：跨进程传输最容易坏的就是「收到的到底是什么」——
            # 是一个 str 还是 list[str]、首元素类型对不对。没有这几个字段，排查只能
            # 靠改代码重跑；有了它们，一次回包就能定性。
            respond(
                {
                    "id": request.get("id"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "diagnostic": {
                        "texts_type": type(texts).__name__,
                        "texts_len": len(texts),
                        "first_type": type(texts[0]).__name__ if texts else None,
                        "first_repr": repr(texts[0])[:120] if texts else None,
                        "stdin_encoding": (sys.stdin.encoding or "?"),
                    },
                }
            )
            continue
        payload, dims = _encode(vectors)
        respond(
            {
                "id": request.get("id"),
                "dims": dims,
                "count": len(vectors),
                "vectors_b64": payload,
                "embed_seconds": round(time.perf_counter() - begin, 4),
                "rss_mb": _rss_mb(),
            }
        )
    return 0


# ------------------------------------------------------------------ 第 3 段：实测


def _venv_python(venv_dir: pathlib.Path) -> pathlib.Path:
    return venv_dir / ("Scripts" if sys.platform == "win32" else "bin") / (
        "python.exe" if sys.platform == "win32" else "python"
    )


def probe_measure(model_name: str) -> int:
    """建独立 venv → 装 fastembed → 以子进程协议实测四项指标。"""
    venv_dir = PROBE_DIR / "venv"
    PROBE_DIR.mkdir(parents=True, exist_ok=True)

    if not _venv_python(venv_dir).exists():
        print(f"[1/3] 建独立 venv：{venv_dir}")
        code, out = _run(
            ["uv", "venv", str(venv_dir), "--python", f"{sys.version_info.major}.{sys.version_info.minor}"],
            300.0,
        )
        if code != 0:
            print("  建 venv 失败：", out.strip()[:400])
            return 1
    else:
        print(f"[1/3] 复用已有 venv：{venv_dir}")

    python = _venv_python(venv_dir)
    print("[2/3] 安装 fastembed（首次会下载模型运行时，可能较慢）")
    code, out = _run(
        ["uv", "pip", "install", "--python", str(python), "fastembed"], 900.0
    )
    if code != 0:
        print("  安装失败：", out.strip()[-800:])
        return 1

    print(f"[3/3] 以子进程协议实测（模型 {model_name}，首次会下载权重）")
    process = subprocess.Popen(
        [str(python), str(pathlib.Path(__file__).resolve()), "--child", "--model", model_name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None

    started = time.perf_counter()
    ready_line = process.stdout.readline()
    spawn_seconds = time.perf_counter() - started
    try:
        ready = json.loads(ready_line)
    except json.JSONDecodeError:
        print("  子进程未就绪：", ready_line[:400])
        process.kill()
        return 1
    if "error" in ready:
        print("  子进程报错：", ready)
        process.kill()
        return 1

    texts = [f"第 {index} 段中文测试文本，用于测量批量嵌入吞吐。" for index in range(32)]
    process.stdin.write(json.dumps({"id": 1, "texts": texts}, ensure_ascii=False) + "\n")
    process.stdin.flush()
    single_started = time.perf_counter()
    response = json.loads(process.stdout.readline())
    first_call_seconds = time.perf_counter() - single_started

    print("\n=== 实测结果 ===")
    print(f"  子进程就绪（含模型加载）：{spawn_seconds:.2f} s")
    print(f"  模型加载耗时            ：{ready.get('load_seconds')} s")
    print(f"  加载后常驻内存          ：{ready.get('rss_mb')} MB")
    if "error" in response:
        print("  首次嵌入失败：", response["error"])
        process.kill()
        return 1
    print(f"  批量 32 条（含首次推理）：{first_call_seconds:.3f} s")
    print(f"    模型侧耗时            ：{response.get('embed_seconds')} s")
    print(f"    维度                  ：{response.get('dims')}")

    vectors = _decode(response["vectors_b64"], int(response["dims"]), int(response["count"]))
    norm = sum(value * value for value in vectors[0]) ** 0.5
    print(f"    首条向量模长          ：{norm:.4f}（接近 1 说明已归一化，余弦可退化为点积）")

    process.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
    process.stdin.flush()
    process.wait(timeout=30)
    return 0


# ------------------------------------------------------------------ 入口


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="嵌入后端开工前探针")
    parser.add_argument("--measure", action="store_true", help="追加实测（会建 venv 并下载模型）")
    parser.add_argument("--child", action="store_true", help="以内嵌服务模式运行（由 --measure 调用）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"测量用模型（默认 {DEFAULT_MODEL}）")
    args = parser.parse_args(argv)

    if args.child:
        return child_loop(args.model)

    failures: list[str] = []
    skipped = False

    print("=== 第 1 段：轮子可用性（本机 Python %d.%d） ===" % (sys.version_info.major, sys.version_info.minor))
    # WHY 在函数内 import：``--child`` 模式跑在**只有模型运行时**的独立 venv 里，
    # 那里没有 httpx；模块级 import 会让子进程直接 ImportError。
    import httpx

    with httpx.Client(timeout=30.0) as client:
        wheel_reports = probe_wheels(client)
    for item in wheel_reports:
        mark = "OK  " if item["ok"] else "NO  "
        print(f"  [{mark}] {item['name']:<28} {item['version']:<12} {item['note']}")
        if not item["ok"] and item["name"] in {"fastembed", "onnxruntime"}:
            skipped = True

    print("\n=== 第 2 段：Docker 与候选镜像 ===")
    image_reports = probe_docker()
    for item in image_reports:
        mark = "OK  " if item["ok"] else "NO  "
        print(f"  [{mark}] {item['image']:<48} {item['note']}")

    if args.measure:
        print("\n=== 第 3 段：实测量 ===")
        return probe_measure(args.model)

    docker_ok = any(item["ok"] and item["image"] == "(docker daemon)" for item in image_reports)
    print("\n=== 结论 ===")
    print(f"  轮子：{'有候选可用' if not skipped else 'onnxruntime / fastembed 在当前 Python 上无可用轮子'}")
    print(f"  Docker：{'可用' if docker_ok else '不可用'}")
    print("  下一步：若轮子可用 → `--measure` 取实测量；若不可用 → 走容器路线（第 2 段已确认镜像可达）")
    return 1 if failures else (2 if skipped else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
