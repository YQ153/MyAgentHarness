"""子进程嵌入客户端：协议往返、失败路径与生命周期。

WHY 用替身服务而不是真模型：这一层要验的是**协议与进程管理**（分批、一问一答、
超时、空闲回收、编码保真），而真模型需要 189 MB 权重与一次冷下载，既慢又让失败原因
不可分辨。真模型链路由 ``scripts/smoke_embed_backend.py`` 在真机上单独验证。

替身按 ``--model`` 收到的值切换行为——客户端只留了这一个可透传的入口，替身借此免去
为测试在生产协议里开一个口子。
"""

from __future__ import annotations

import asyncio
import base64
import struct
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

from llm.embed_process import EmbedProcessClient, EmbedProcessError, _decode_vectors

_STUB_SERVER = r'''
"""协议替身服务：按 --model 传进来的模式决定行为。"""

import base64
import json
import struct
import sys
import time

MODE = sys.argv[sys.argv.index("--model") + 1]

# 与生产服务同样显式重配 UTF-8：替身若省掉这一步，就测不出编码保真
sys.stdin.reconfigure(encoding="utf-8", errors="replace")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def respond(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if MODE == "crash-on-start":
    print("ImportError: No module named 'onnxruntime'", file=sys.stderr, flush=True)
    sys.exit(3)

if MODE == "load-fail":
    message = "ImportError: No module named 'fastembed'"
    print(message, file=sys.stderr, flush=True)
    respond({"event": "error", "error": message})
    sys.exit(1)

respond({"event": "ready", "model": MODE, "load_seconds": 0.01, "rss_mb": 1.0})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    if request.get("op") == "shutdown":
        respond({"id": request.get("id"), "ok": True})
        sys.exit(0)

    texts = request["texts"]
    request_id = request.get("id")

    if MODE == "error":
        respond({"id": request_id, "error": "boom"})
        continue
    if MODE == "exit-early":
        sys.exit(4)
    if MODE == "bad-json":
        sys.stdout.write("这不是 JSON\n")
        sys.stdout.flush()
        continue
    if MODE == "slow":
        time.sleep(10)
    if MODE == "max-2" and len(texts) > 2:
        respond({"id": request_id, "error": "too many texts"})
        continue

    dims = 2 if MODE == "wrong-dims" else 3
    # 每个分量都由收到的文本算出：编码只要错一步，值就对不上
    vectors = [[float(sum(ord(c) for c in text)), float(len(text)), 1.0][:dims] for text in texts]
    flat = [value for vector in vectors for value in vector]
    payload = base64.b64encode(struct.pack("<" + str(len(flat)) + "f", *flat)).decode("ascii")
    respond({"id": request_id, "dims": dims, "count": len(vectors), "vectors_b64": payload})
'''


@pytest.fixture
def stub_server(tmp_path: Path) -> Path:
    """把协议替身写到临时目录并返回其路径。"""
    path = tmp_path / "stub_embed_server.py"
    path.write_text(_STUB_SERVER, encoding="utf-8")
    return path


@pytest.fixture
async def make_client(stub_server: Path) -> Any:
    """按模式构造客户端；用例结束后统一关闭，避免留下孤儿进程。"""
    created: list[EmbedProcessClient] = []

    def _make(mode: str = "ok", **overrides: Any) -> EmbedProcessClient:
        params: dict[str, Any] = {
            "python": Path(sys.executable),
            "server": stub_server,
            "model": mode,
            "dims": 3,
            "timeout": 20.0,
            "idle_seconds": 0,
        }
        params.update(overrides)
        client = EmbedProcessClient(**params)
        created.append(client)
        return client

    yield _make

    for client in created:
        await client.aclose()


# --------------------------------------------------------------- 协议往返


async def test_roundtrip_preserves_chinese_text(make_client: Callable[..., EmbedProcessClient]) -> None:
    """中文经管道往返后不得变形——回归 Windows 管道按 GBK 解码的缺陷。

    WHY 用「码点和」而不是断言回显的字符串：向量是按**收到的文本**算出来的，因此
    管线里任何一步编码错了这个数都对不上。它把「协议是否保真」变成一次可精确比较的
    断言，而不需要让生产协议为了回显多带一个字段。

    WHY 这条用例值得单独立：该缺陷**纯 ASCII 时完全正常**（探针第一轮 32 条英文全过、
    换中文才炸），若某条路径不报错而是接受了乱码，索引会被静默写歪。
    """
    texts = ["中文测试段落", "第二段更长的中文文本"]
    client = make_client()

    vectors = await client.embed(texts)

    assert [vector[0] for vector in vectors] == [float(sum(map(ord, text))) for text in texts]
    assert [vector[1] for vector in vectors] == [float(len(text)) for text in texts]


async def test_embed_batches_requests(make_client: Callable[..., EmbedProcessClient]) -> None:
    """单次请求的条数不超过 ``batch_size``。

    WHY 用「替身拒绝超量请求」来断言分批，而不是统计调用次数：替身一旦收到超量请求
    就回错，因此只要客户端没分批这条用例必然失败——判据落在协议行为上，不需要为了
    可测性在生产代码里加计数。
    """
    client = make_client("max-2", batch_size=2)

    vectors = await client.embed(["a", "b", "c", "d", "e"])

    assert len(vectors) == 5


async def test_empty_input_does_not_start_the_process(
    make_client: Callable[..., EmbedProcessClient],
) -> None:
    """空入参不应唤起子进程——加载模型的代价不该为零条文本付一次。"""
    client = make_client()

    assert await client.embed([]) == []
    assert client.running is False


async def test_process_is_started_lazily_and_reused(
    make_client: Callable[..., EmbedProcessClient],
) -> None:
    """首次调用才启动，后续调用复用同一个进程。"""
    client = make_client()
    assert client.running is False

    await client.embed(["a"])
    assert client.running is True

    await client.embed(["b"])
    assert client.running is True


# --------------------------------------------------------------- 失败路径


async def test_child_error_is_raised(make_client: Callable[..., EmbedProcessClient]) -> None:
    """子进程回 ``error`` 时抛出并带上原因。"""
    client = make_client("error")

    with pytest.raises(EmbedProcessError, match="boom"):
        await client.embed(["a"])


async def test_child_exit_midway_is_reported(
    make_client: Callable[..., EmbedProcessClient],
) -> None:
    """未返回响应即退出时，报错要说明「提前退出」而不是抛一条解码异常。"""
    client = make_client("exit-early")

    with pytest.raises(EmbedProcessError, match="提前退出"):
        await client.embed(["a"])

    assert client.running is False


async def test_malformed_response_is_reported(
    make_client: Callable[..., EmbedProcessClient],
) -> None:
    """响应不是 JSON 时要给出可读原因。"""
    client = make_client("bad-json")

    with pytest.raises(EmbedProcessError, match="不是 JSON"):
        await client.embed(["a"])


async def test_model_load_failure_surfaces_the_reason(
    make_client: Callable[..., EmbedProcessClient],
) -> None:
    """子进程加载模型失败时，把它的报错原文带上来。

    WHY 关键：独立模型环境最常见的失败就是缺依赖，而原因只有子进程知道。不把它带
    上来，用户看到的只是一句「启动失败」。
    """
    client = make_client("load-fail")

    with pytest.raises(EmbedProcessError, match="fastembed"):
        await client.embed(["a"])


async def test_crash_without_handshake_is_reported(
    make_client: Callable[..., EmbedProcessClient],
) -> None:
    """进程在握手前就退出（无任何 stdout）时也要有明确报错。"""
    client = make_client("crash-on-start")

    with pytest.raises(EmbedProcessError, match="启动后立即退出"):
        await client.embed(["a"])


async def test_missing_interpreter_points_at_the_setup_script(
    stub_server: Path, tmp_path: Path
) -> None:
    """解释器不存在时，报错要直接给出准备环境的命令。"""
    client = EmbedProcessClient(
        python=tmp_path / "no-such-venv" / "python",
        server=stub_server,
        model="ok",
        dims=3,
    )

    with pytest.raises(EmbedProcessError, match="setup_embed_venv"):
        await client.embed(["a"])


async def test_dim_mismatch_from_child_is_rejected(
    make_client: Callable[..., EmbedProcessClient],
) -> None:
    """子进程返回的维度与配置不符时要拦下，而不是把短向量写进索引。"""
    client = make_client("wrong-dims")

    with pytest.raises(EmbedProcessError, match="形状不符"):
        await client.embed(["a"])


async def test_timeout_terminates_the_process(
    make_client: Callable[..., EmbedProcessClient],
) -> None:
    """请求超时后必须终止子进程。

    WHY：超时后的进程状态未知（可能仍在推理），复用它会让**下一次**请求读到本次的
    迟到响应——那是一次看起来完全正常的错误结果。
    """
    client = make_client("slow", timeout=0.5)

    with pytest.raises(EmbedProcessError, match="超时"):
        await client.embed(["a"])

    assert client.running is False


# --------------------------------------------------------------- 生命周期


async def test_aclose_stops_the_process(
    make_client: Callable[..., EmbedProcessClient],
) -> None:
    """关闭后进程不再存活。"""
    client = make_client()
    await client.embed(["a"])
    assert client.running is True

    await client.aclose()

    assert client.running is False


async def test_idle_shutdown_reclaims_the_process(
    make_client: Callable[..., EmbedProcessClient],
) -> None:
    """空闲到期后回收进程——这是「子进程档位」相对进程内模型的主要收益。"""
    client = make_client(idle_seconds=1)
    await client.embed(["a"])
    assert client.running is True

    for _ in range(60):
        if not client.running:
            break
        await asyncio.sleep(0.1)

    assert client.running is False


async def test_process_restarts_after_idle_shutdown(
    make_client: Callable[..., EmbedProcessClient],
) -> None:
    """回收后再调用要能重新拉起，而不是留下一个「用过一次就废」的后端。"""
    client = make_client(idle_seconds=1)
    await client.embed(["a"])
    for _ in range(60):
        if not client.running:
            break
        await asyncio.sleep(0.1)
    assert client.running is False

    vectors = await client.embed(["abc"])

    assert client.running is True
    assert vectors[0][1] == 3.0


# --------------------------------------------------------------- 向量解码


def test_decode_vectors_roundtrip() -> None:
    """合法载荷要能精确还原。"""
    flat = [0.5, -1.25, 3.0]
    payload = base64.b64encode(struct.pack("<3f", *flat)).decode("ascii")

    vectors = _decode_vectors({"dims": 3, "count": 1, "vectors_b64": payload}, 3, 1)

    assert vectors == [[0.5, -1.25, 3.0]]


def test_decode_vectors_rejects_shape_mismatch() -> None:
    """条数或维度不符时报错。"""
    with pytest.raises(EmbedProcessError, match="形状不符"):
        _decode_vectors({"dims": 2, "count": 1, "vectors_b64": ""}, 3, 1)
    with pytest.raises(EmbedProcessError, match="形状不符"):
        _decode_vectors({"dims": 3, "count": 2, "vectors_b64": ""}, 3, 1)


def test_decode_vectors_rejects_truncated_payload() -> None:
    """载荷长度与形状不符时报错，而不是解出一个长度不对的向量。"""
    payload = base64.b64encode(struct.pack("<2f", 1.0, 2.0)).decode("ascii")

    with pytest.raises(EmbedProcessError, match="载荷长度不符"):
        _decode_vectors({"dims": 3, "count": 1, "vectors_b64": payload}, 3, 1)


def test_decode_vectors_rejects_missing_payload() -> None:
    """缺少载荷字段时报错。"""
    with pytest.raises(EmbedProcessError, match="vectors_b64"):
        _decode_vectors({"dims": 3, "count": 1}, 3, 1)


def test_construction_rejects_bad_arguments(tmp_path: Path) -> None:
    """维度与批大小必须是正数，在构造期就拦下。"""
    with pytest.raises(ValueError, match="dims"):
        EmbedProcessClient(python=tmp_path / "p", server=tmp_path / "s", model="m", dims=0)
    with pytest.raises(ValueError, match="batch_size"):
        EmbedProcessClient(
            python=tmp_path / "p", server=tmp_path / "s", model="m", dims=3, batch_size=0
        )
