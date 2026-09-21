"""嵌入服务瘦进程：在独立 venv 里加载模型，按 stdio 提供嵌入。

它由 ``llm/embed_process.py`` 启动，**运行在只有模型运行时的独立环境里**，因此
不得 import 任何项目模块（会立刻 ImportError）。协议与客户端一一对应：
行分隔 JSON，请求 ``{"id", "texts": [...]}``，响应带 ``vectors_b64``。

用法（由客户端调用，一般不需要手工执行）::

    .data/embed-venv/Scripts/python scripts/embed_server.py --model BAAI/bge-small-zh-v1.5

手工执行时它从 stdin 读 JSON 行、向 stdout 写 JSON 行，可直接用来验证模型环境。

退出方式：收到 ``{"op": "shutdown"}``，或 stdin 关闭（EOF）——后者保证父进程一旦
退出，本进程不会变成需要人工清理的孤儿。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import struct
import sys
import time


def _reconfigure_stdio() -> None:
    """把 stdin/stdout 显式重配为 UTF-8。

    WHY 必须做：Windows 上子进程的 stdio 默认跟随 **ANSI 代码页**（本机 GBK），而父
    进程按 UTF-8 写字节——中文会被按 GBK 解码成乱码并产生孤立代理项，tokenizer 随即
    拒绝。这个缺陷**只用 ASCII 测不出来**（探针里 32 条英文全过、换成中文才炸），
    而更糟的情形是不报错：乱码进了向量，索引长期悄悄歪掉。

    WHY 由子进程自己重配而不是靠父进程设 ``PYTHONIOENCODING``：协议的编码只有一处
    真相，手工在别的环境里拉起本脚本时行为也一致。
    """
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _rss_mb() -> float | None:
    """当前进程常驻内存（MB）；取不到时返回 ``None`` 而不是猜一个数。

    WHY 要报这个数：它正是「子进程档位」存在的主要理由（常驻 189 MB 可被整体回收），
    没有这个数就只能靠外部工具去量。
    """
    try:
        import resource

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
        # WHY 两个入口都试：K32GetProcessMemoryInfo 在新系统上由 kernel32 导出，
        # psapi 的旧名在部分环境下才在。任一成功即可，都失败就如实返回 None。
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process = kernel32.GetCurrentProcess()
        except OSError:
            return None
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

    WHY 不用 JSON 数字数组：千级分块的向量编成数字数组是几十 MB 文本，base64 只要
    约 8 MB——这个差别直接决定索引是几秒还是几分钟。
    """
    if not vectors:
        return "", 0
    dims = len(vectors[0])
    flat = [value for vector in vectors for value in vector]
    return base64.b64encode(struct.pack(f"<{len(flat)}f", *flat)).decode("ascii"), dims


def _load_model(model_name: str) -> object:
    """加载并预热模型。

    Raises:
        ImportError: 当前环境没有 ``fastembed``。
    """
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name=model_name)
    # WHY 预热一次：onnxruntime 的首次推理含图优化与内存分配，不预热会把它的耗时
    # 算进「冷启动」，让上游据此定出的超时阈值过高。
    list(model.embed(["warmup"]))
    return model


def main(argv: list[str]) -> int:
    """按行处理请求，直到 shutdown 或 stdin EOF。

    Returns:
        进程退出码；``0`` 表示正常收尾。
    """
    _reconfigure_stdio()

    parser = argparse.ArgumentParser(description="MyAgentHarness 嵌入服务（stdio）")
    parser.add_argument("--model", required=True, help="fastembed 模型标识")
    args = parser.parse_args(argv)

    def respond(payload: dict[str, object]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    started = time.perf_counter()
    try:
        model = _load_model(args.model)
    except Exception as exc:
        # WHY 把加载失败当成一次「响应」而不是直接崩：客户端在等 ready 行，进程静默
        # 退出只会让它看到 EOF。带上类型与消息（并写进 stderr）能让「缺 fastembed」
        # 这类最常见的原因直接被看见。
        message = f"{type(exc).__name__}: {exc}"
        print(message, file=sys.stderr, flush=True)
        respond({"event": "error", "model": args.model, "error": message})
        return 1

    respond(
        {
            "event": "ready",
            "model": args.model,
            "load_seconds": round(time.perf_counter() - started, 3),
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

        texts = request.get("texts")
        if not isinstance(texts, list):
            respond({"id": request.get("id"), "error": "请求缺少 texts 数组"})
            continue

        try:
            vectors = [list(map(float, item)) for item in model.embed(texts)]
        except Exception as exc:
            # WHY 带上输入形态：跨进程传输最容易坏的就是「收到的到底是什么」——
            # 一个 str 还是 list[str]、首元素类型对不对。没有这几个字段，排查只能
            # 靠改代码重跑。
            respond(
                {
                    "id": request.get("id"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "diagnostic": {
                        "texts_type": type(texts).__name__,
                        "texts_len": len(texts),
                        "first_type": type(texts[0]).__name__ if texts else None,
                        "first_repr": repr(texts[0])[:120] if texts else None,
                        "stdin_encoding": sys.stdin.encoding or "?",
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
                "rss_mb": _rss_mb(),
            }
        )

    # stdin 关闭（父进程退出）：正常收尾，不留孤儿
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
