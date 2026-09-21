"""真机冒烟：附件上传、预览与「模型不支持图片时必须显式拒绝」。

验证顺序（前三项不需要任何多模态模型）：

1. ``GET /api/attachments/limits`` 可用——前端要在草稿态就能做选文件校验；
2. 在真实工作区里上传一张真实 PNG，并用工作区文件接口把它取回来（证明落盘的是
   一个可预览的图片，而不是一段字节）；
3. 用一个**不支持图片**的模型带附件发起运行 → **必须 400**，且错误里点名了模型与
   可用的多模态模型。这一条是 T21 的红线：静默丢弃图片比报错危险得多；
4. 若本机注册了多模态模型，则真的跑一轮（会调用模型）；没有则跳过并返回退出码 2。

WHY 这里不验证「删除会话会清掉附件」：本脚本创建的会话是**草稿**（``POST /api/threads``
只生成 ID，元数据到首轮运行才落库），而草稿没有可删除的记录。那条接线由
``tests/application/test_thread_service.py::test_delete_removes_attachments`` 覆盖；
草稿态附件无法回收本身是一条已知限制（见 README 第九章）。

WHY 走真实应用而不是直接调服务：要验证的是「路由 → 应用服务 → 存储 → 工作区」这条
完整接线，直接调服务会把路由层的状态码映射与权限依赖排除在验收之外。

用法：
    python scripts/smoke_attachments.py

退出码：``0`` 全通 / ``2`` 有跳过（无多模态模型）/ ``1`` 有失败。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import struct
import sys
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
# WHY 强制 UTF-8 输出：Windows 控制台默认 GBK，模型回复里出现非 GBK 字符时 print
# 会抛 UnicodeEncodeError，把冒烟结论变成一条假失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from config import AppConfig  # noqa: E402
from interfaces.web.app import create_app  # noqa: E402

logger = logging.getLogger("smoke.attachments")


def _tiny_png() -> bytes:
    """生成一张真实的 1x1 红色 PNG。

    WHY 手工拼字节而不是引 Pillow：冒烟脚本不该为一个 67 字节的常量引入图像库，
    而「是不是一张真图片」恰恰是本脚本要验证的东西之一。
    """
    raw = b"\x00" + bytes([255, 0, 0])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

    # WHY 走 ``load()``：工作区必填，直接构造在未配置时只会抛 pydantic 原文。
    config = AppConfig.load()
    app = create_app(config)
    failures: list[str] = []
    skipped = False

    # WHY 手动进入 lifespan：httpx 的 ASGITransport 不会代跑它，而附件服务、数据库
    # 与检查点都只在 lifespan 里装配；不跑就会得到一屏「服务未初始化」的 503。
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://smoke", timeout=120.0
        ) as http:
            print("=== 1. 上限查询 ===")
            limits_response = await http.get("/api/attachments/limits")
            print(limits_response.status_code, limits_response.text[:200])
            if limits_response.status_code != 200:
                failures.append("上限查询未返回 200")
                limits = {}
            else:
                limits = limits_response.json()

            models_response = await http.get("/api/models")
            models = models_response.json() if models_response.status_code == 200 else []
            vision_models = [item["name"] for item in models if item.get("supports_vision")]
            print("已注册模型：", [(item["name"], item.get("supports_vision")) for item in models])

            print("\n=== 2. 上传并回取 ===")
            created = await http.post("/api/threads")
            thread_id = created.json()["thread_id"]
            uploaded = await http.post(
                f"/api/threads/{thread_id}/attachments",
                files={"file": ("smoke.png", _tiny_png(), "image/png")},
            )
            print(uploaded.status_code, uploaded.text[:200])
            if uploaded.status_code != 200:
                failures.append("上传未返回 200")
                attachment = {}
            else:
                attachment = uploaded.json()

            if attachment:
                fetched = await http.get(
                    "/api/workspace/file",
                    params={"path": attachment["path"]},
                )
                payload = fetched.json()
                print("回取：kind=%s size=%s" % (payload.get("kind"), payload.get("size")))
                if payload.get("kind") != "image" or not str(payload.get("text", "")).startswith(
                    "data:image/png;base64,"
                ):
                    failures.append("上传后的文件无法作为图片预览")

            print("\n=== 3. 非多模态模型必须显式拒绝 ===")
            text_models = [item["name"] for item in models if not item.get("supports_vision")]
            if not attachment or not text_models:
                failures.append("缺少可用的纯文本模型或附件，无法验证拒绝路径")
            else:
                rejected = await http.post(
                    f"/api/threads/{thread_id}/runs",
                    json={
                        "content": "描述这张图",
                        "model": text_models[0],
                        "attachment_ids": [attachment["id"]],
                    },
                )
                print(rejected.status_code, rejected.text[:300])
                detail = ""
                try:
                    detail = rejected.json().get("detail", "")
                except json.JSONDecodeError:
                    detail = rejected.text
                if rejected.status_code != 400 or text_models[0] not in detail:
                    failures.append("非多模态模型未按要求显式拒绝")

            print("\n=== 4. 多模态模型端到端（可选）===")
            if not vision_models:
                print("[SKIP] 本机没有注册多模态模型（见 VISION_MODEL_ALIASES 与对应密钥）")
                skipped = True
            else:
                created = await http.post("/api/threads")
                thread_id = created.json()["thread_id"]
                uploaded = await http.post(
                    f"/api/threads/{thread_id}/attachments",
                    files={"file": ("smoke.png", _tiny_png(), "image/png")},
                )
                attachment = uploaded.json()
                answer_parts: list[str] = []
                async with http.stream(
                    "POST",
                    f"/api/threads/{thread_id}/runs",
                    json={
                        "content": "这张图是什么颜色？只回答颜色。",
                        "model": vision_models[0],
                        "attachment_ids": [attachment["id"]],
                    },
                ) as stream:
                    print("运行 HTTP 状态：", stream.status_code)
                    if stream.status_code != 200:
                        body = (await stream.aread()).decode("utf-8", errors="replace")
                        failures.append(f"多模态运行未返回 200：{body[:200]}")
                    else:
                        async for line in stream.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                event = json.loads(line[5:].strip())
                            except json.JSONDecodeError:
                                continue
                            if event.get("type") == "token":
                                answer_parts.append(str(event.get("text", "")))
                answer = "".join(answer_parts).strip()
                print("模型回答：", answer[:200])
                if not answer:
                    failures.append("多模态运行没有产生回答")

    print("\n=== 结论 ===")
    if failures:
        for item in failures:
            print("  [FAIL]", item)
        return 1
    if skipped:
        print("  [SKIP] 第 4 项未执行（无多模态模型）；其余检查通过")
        return 2
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
