"""嵌入后端真机冒烟：按当前配置走一遍真实链路。

与 ``tests/llm/test_embed_process.py`` 的分工：那边用替身服务验**协议与进程管理**
（快、不依赖模型），这里用真模型验**配置能不能真的跑起来**——权重要能加载、中文要
能嵌入、维度要与 ``EMBEDDING_DIMS`` 一致、空闲回收要真的把内存还回去。

为什么还要一条语义自检：维度对、模长对，**并不代表向量有意义**。探针阶段就抓到过
「管道按 GBK 解码、中文变成乱码」这类缺陷——它产出的向量形状完全正常，只有比较
语义相近与无关句子的相似度才能发现。故本脚本用「相近句相似度必须高于无关句」作为
最低限度的有效性判据。

用法::

    python scripts/smoke_embed_backend.py                        # 按 .env 的档位
    python scripts/smoke_embed_backend.py --backend subprocess   # 指定档位

退出码：``0`` 通过 / ``2`` 有跳过（未启用嵌入）/ ``1`` 失败。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# WHY 强制 UTF-8：本脚本的核心用例就是中文，Windows 控制台默认 GBK 会让 print 抛
# UnicodeEncodeError，把一次成功的验收变成假失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import AppConfig  # noqa: E402
from llm.embed_process import EmbedProcessClient  # noqa: E402
from llm.embeddings import EmbeddingError, build_embeddings  # noqa: E402

_NEAR_A = "如何重置账号密码"
_NEAR_B = "忘记密码了怎么找回"
_FAR = "明天北京的天气怎么样"
"""语义自检用的三句话：前两句同主题，第三句无关。"""

_IDLE_PROBE_SECONDS = 1
"""空闲回收验证用的短阈值：真机等 600s 不现实，而回收逻辑与阈值大小无关。"""


def _cosine(left: list[float], right: list[float]) -> float:
    """余弦相似度；任一为零向量时返回 0 而不是抛除零。"""
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


async def _check_idle_reclaim(config: AppConfig, backend: EmbedProcessClient) -> str | None:
    """验证子进程会在空闲后真的被回收。

    WHY 单独起一个短阈值的客户端：回收逻辑与阈值大小无关，而真机等默认的 600s 不
    现实。复用已下载的权重，因此这次启动只需一两秒。

    Returns:
        ``None`` 表示通过，否则返回失败原因。
    """
    probe = EmbedProcessClient(
        python=backend.python,
        server=backend.server,
        model=backend.model,
        dims=backend.dims,
        timeout=config.embedding_timeout_seconds,
        idle_seconds=_IDLE_PROBE_SECONDS,
    )
    try:
        await probe.embed([_NEAR_A])
        if not probe.running:
            return "刚嵌入完进程就已不在运行"
        for _ in range(60):
            if not probe.running:
                print(f"  [OK  ] 空闲 {_IDLE_PROBE_SECONDS}s 后进程已回收")
                return None
            await asyncio.sleep(0.1)
        return f"空闲 {_IDLE_PROBE_SECONDS}s 后进程仍在运行——回收没生效"
    finally:
        await probe.aclose()


async def _run(config: AppConfig) -> int:
    """执行各项检查，返回退出码。"""
    backend = build_embeddings(config)
    if backend is None:
        print("[SKIP] EMBEDDING_BACKEND=none：未启用嵌入，先配置档位再跑本脚本")
        return 2

    steps = 5 if isinstance(backend, EmbedProcessClient) else 4
    failures: list[str] = []

    print(f"[1/{steps}] 后端：{backend.name}（dims={backend.dims}）")

    print(f"[2/{steps}] 嵌入中文文本")
    try:
        vectors = await backend.embed([_NEAR_A, _NEAR_B, _FAR])
    except EmbeddingError as exc:
        print(f"  [FAIL] 嵌入失败：{exc}")
        await backend.aclose()
        return 1

    shapes_ok = len(vectors) == 3 and all(len(vector) == config.embedding_dims for vector in vectors)
    if len(vectors) != 3:
        failures.append(f"返回条数不符：期望 3，实际 {len(vectors)}")
    elif not shapes_ok:
        failures.append(
            f"维度与 EMBEDDING_DIMS 不符：期望 {config.embedding_dims}，实际 {len(vectors[0])}"
        )
    else:
        print(f"  [OK  ] 3 条 × {len(vectors[0])} 维")

    print(f"[3/{steps}] 语义有效性（相近句相似度应高于无关句）")
    if shapes_ok:
        near = _cosine(vectors[0], vectors[1])
        far = _cosine(vectors[0], vectors[2])
        print(f"  相近句 cos={near:.4f}，无关句 cos={far:.4f}")
        if near <= far:
            failures.append(
                f"语义不自洽：相近句 {near:.4f} 未高于无关句 {far:.4f}——"
                "向量可能无意义（例如文本在传输过程中被破坏）"
            )
        else:
            print("  [OK  ] 相似度顺序正确")
    else:
        failures.append("向量形状不一致，跳过语义检查")

    if isinstance(backend, EmbedProcessClient):
        print(f"[4/{steps}] 空闲回收（该档位的主要收益）")
        reason = await _check_idle_reclaim(config, backend)
        if reason is None:
            pass
        else:
            print(f"  [FAIL] {reason}")
            failures.append(reason)

    print(f"[{steps}/{steps}] 释放资源")
    await backend.aclose()
    print("  [OK  ] 已关闭")

    if failures:
        for item in failures:
            print(f"[FAIL] {item}")
        return 1
    print("\n[PASS] 嵌入后端真机冒烟通过")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="嵌入后端真机冒烟")
    parser.add_argument(
        "--backend",
        default=None,
        choices=["none", "openai-compat", "subprocess"],
        help="覆盖 EMBEDDING_BACKEND（默认取 .env / 环境变量）",
    )
    args = parser.parse_args(argv)

    # WHY 两个分支都走 ``load()``：工作区是必填项，直接构造在未配置时只会抛 pydantic
    # 原文；``load()`` 给出「该写哪个变量」的提示，并顺带把目录建好。
    config = AppConfig.load(embedding_backend=args.backend)
    print(
        f"配置：backend={config.embedding_backend} model={config.embedding_model} "
        f"dims={config.embedding_dims}"
    )
    return asyncio.run(_run(config))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
