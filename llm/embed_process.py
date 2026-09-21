"""子进程嵌入客户端：在独立 venv 里托管模型，按 stdio 协议通信。

WHY 让模型住进子进程而不是主进程：
- 主进程不必安装模型运行时与权重——Web 的依赖与模型的依赖解耦；
- 模型实测常驻 189 MB，而知识库检索是**偶发**动作，子进程能在空闲时被整个终止，
  把内存真正还给操作系统（进程内的对象做不到这一点）；
- 模型崩溃或卡死不会带走 Web 进程。

协议（行分隔 JSON，与 ``scripts/embed_server.py`` 一一对应）::

    启动   ->  子进程先输出一行 {"event": "ready", ...}
    请求   ->  {"id": 1, "texts": ["...", ...]}
    响应   ->  {"id": 1, "dims": 512, "count": 2, "vectors_b64": "..."}
    关闭   ->  {"op": "shutdown"}  ->  {"id": 2, "ok": true}

WHY 向量用 base64(float32) 而不是 JSON 数字数组：千级分块的向量编成数字数组是几十
MB 文本，base64 只要约 8 MB——这个差别直接决定一次索引是几秒还是几分钟。

WHY 不自己跟踪「父进程还活着」：子进程按行读 stdin，父进程一旦退出管道即关闭，读
循环自然结束并退出。用 pid 文件或心跳实现同一件事，多出来的状态反而会在异常退出后
留下需要人工清理的残留。

WHY 编码处处显式 UTF-8：Windows 上管道默认跟随 ANSI 代码页（本机 GBK），中文会被
按 GBK 解码成乱码并产生孤立代理项。这类缺陷**只用 ASCII 测不出来**——探针第一轮 32
条英文全过、换中文才炸；若某条路径不报错而是接受了乱码，就会把语义完全错误的向量
静默写进索引。父进程按 UTF-8 编码字节、子进程自我重配 UTF-8，两侧都不依赖 locale。
"""

from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import struct
import sys
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

_READY_TIMEOUT_SECONDS = 600.0
"""等待子进程 ready 的上限。

WHY 给到 10 分钟而不是复用请求超时：首次启动需要下载模型权重（探针实测冷下载
84.5 s），这是一次性开销。用请求级超时（默认 30 s）等它，会让「第一次用知识库必然
超时」变成一条看起来像故障的现象。
"""

_TERMINATE_GRACE_SECONDS = 5.0
"""向子进程发出终止信号后，等待它自行退出的宽限期。"""

_STDERR_TAIL_LINES = 20
"""保留的子进程 stderr 末几行，用于在失败时给出可诊断的原因。"""


class EmbedProcessError(RuntimeError):
    """子进程嵌入失败（启动、通信或推理）。

    WHY 单独一个异常类型：索引与检索两条路径都要能区分「嵌入不可用」与「数据库
    出错」——前者应降级为关键词检索并告警，后者是必须暴露的故障。
    """


def _decode_vectors(response: dict[str, object], dims: int, expected_count: int) -> list[list[float]]:
    """把响应里的 base64(float32) 还原成向量列表，并校验形状。

    Raises:
        EmbedProcessError: 维度、条数或载荷长度与请求不符。
    """
    reported_dims = response.get("dims")
    reported_count = response.get("count")
    if reported_dims != dims or reported_count != expected_count:
        raise EmbedProcessError(
            f"嵌入响应形状不符：期望 {expected_count}×{dims}，实际 {reported_count}×{reported_dims}"
        )

    payload = response.get("vectors_b64")
    if not isinstance(payload, str):
        raise EmbedProcessError("嵌入响应缺少 vectors_b64 字段")

    raw = base64.b64decode(payload)
    expected_bytes = dims * expected_count * 4
    if len(raw) != expected_bytes:
        raise EmbedProcessError(f"向量载荷长度不符：期望 {expected_bytes} 字节，实际 {len(raw)}")

    flat = struct.unpack(f"<{dims * expected_count}f", raw)
    return [list(flat[index * dims : (index + 1) * dims]) for index in range(expected_count)]


class EmbedProcessClient:
    """按「批量文本进出」契约工作的子进程嵌入客户端。

    满足 ``llm.embeddings.EmbeddingBackend`` 的成员约定（``name`` / ``dims`` /
    ``embed`` / ``aclose``），但**刻意不 import 那个协议**：``llm`` 是模型层，
    结构匹配即可，不必为此建立一条导入关系。

    Attributes:
        model: 模型标识，同时透传给子进程与 ``name``。
        dims: 向量维度；与 ``config.embedding_dims`` 一致，用于校验响应形状。
    """

    def __init__(
        self,
        *,
        python: Path,
        server: Path,
        model: str,
        dims: int,
        timeout: float = 30.0,
        idle_seconds: int = 600,
        batch_size: int = 32,
    ) -> None:
        """构造客户端。**不启动进程**——启动发生在首次 ``embed()``。

        Args:
            python: 独立环境里的解释器路径。
            server: ``embed_server.py`` 的路径。
            model: 模型标识。
            dims: 期望的向量维度。
            timeout: 单次往返超时秒数。
            idle_seconds: 空闲回收秒数；``0`` 表示不回收。
            batch_size: 单次请求的文本条数上限。

        Raises:
            ValueError: ``dims`` 或 ``batch_size`` 非正数。
        """
        if dims < 1:
            raise ValueError(f"dims 必须为正整数，实际：{dims}")
        if batch_size < 1:
            raise ValueError(f"batch_size 必须为正整数，实际：{batch_size}")

        self.python = Path(python)
        self.server = Path(server)
        self.model = model
        self.dims = dims
        self.batch_size = batch_size

        self._timeout = timeout
        self._idle_seconds = idle_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr_task: asyncio.Task[None] | None = None
        # WHY 两把锁而不是一把：启动锁只保证「进程不会起两个」，请求锁保证「一问一答
        # 不交错」。合成一把会让并发的嵌入请求在等进程启动时把彼此也串起来，而它们
        # 本可以共用同一个进程顺序执行。
        self._spawn_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._next_id = 0
        self._idle_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        """后端标识，用于日志与能力公示。"""
        return f"subprocess:{self.model}"

    @property
    def running(self) -> bool:
        """子进程当前是否存活。

        WHY 暴露它：空闲回收是这个档位的主要收益，而「到底有没有回收」需要一个可观测
        且可断言的答案；这也让调用方能在不惊动模型的前提下判断是否需要预热。
        """
        process = self._process
        return process is not None and process.returncode is None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量嵌入，顺序与入参一致。

        Args:
            texts: 待嵌入文本。

        Returns:
            与 ``texts`` 等长的向量列表。

        Raises:
            EmbedProcessError: 启动失败、通信中断、超时或响应形状不符。
        """
        if not texts:
            # WHY 空入参直接返回：唤起一个有 189 MB 模型加载成本的进程，只为嵌入
            # 零条文本，是空索引场景下最容易被忽略的一次无谓冷启动。
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            response = await self._request({"texts": batch})
            vectors.extend(_decode_vectors(response, self.dims, len(batch)))
        return vectors

    async def aclose(self) -> None:
        """优雅关闭：先请子进程自行退出，超时则兜底终止。"""
        self._cancel_idle_shutdown()
        process = self._process
        if process is None or process.returncode is not None:
            self._process = None
            return
        try:
            await self._request({"op": "shutdown"})
        except (EmbedProcessError, asyncio.TimeoutError):
            # 优雅路径失败不改变最终结果——下面 _terminate 会兜底；这里吞掉异常是
            # 因为「关不干净」这件事本身已由 _terminate 处理，重复抛出只会让调用方
            # 在关闭流程里再处理一次同样的失败。
            logger.debug("嵌入子进程未响应 shutdown，转为强制终止", exc_info=True)
        await self._terminate("客户端关闭")

    async def _request(self, payload: dict[str, object]) -> dict[str, object]:
        """发送一条请求并等待其响应。

        Raises:
            EmbedProcessError: 超时、管道断开或子进程报错。
        """
        process = await self._ensure_process()

        # WHY 全程持锁：stdin/stdout 是一对管道，没有多路复用——并发发两条请求会让
        # 响应互相错位，而错位**不会报错**，只会把 A 的向量配给 B 的文本。
        async with self._request_lock:
            self._cancel_idle_shutdown()
            try:
                response = await asyncio.wait_for(
                    self._exchange(process, payload), timeout=self._timeout
                )
            except asyncio.TimeoutError as exc:
                # 超时后子进程状态未知（可能仍在推理），复用它会让下一次请求读到本次
                # 的迟到响应——必须终止，让下次调用重新拉起。
                await self._terminate("请求超时")
                raise EmbedProcessError(
                    f"嵌入子进程超时（{self._timeout}s），已终止，下次调用将重新启动"
                ) from exc
            finally:
                self._schedule_idle_shutdown()

        error = response.get("error")
        if error:
            raise EmbedProcessError(f"嵌入子进程报错：{error}")
        return response

    async def _exchange(
        self, process: asyncio.subprocess.Process, payload: dict[str, object]
    ) -> dict[str, object]:
        """在管道上完成一次「写一行、读一行」。

        Raises:
            EmbedProcessError: 管道不可用/断开，或响应不是合法 JSON、id 不匹配。
        """
        if process.stdin is None or process.stdout is None:
            raise EmbedProcessError("嵌入子进程的管道不可用")

        self._next_id += 1
        request_id = self._next_id
        line = json.dumps({"id": request_id, **payload}, ensure_ascii=False)

        try:
            process.stdin.write((line + "\n").encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self._terminate("输入管道断开")
            raise EmbedProcessError(
                f"嵌入子进程的输入管道已断开（{type(exc).__name__}），已终止，下次调用将重新启动"
            ) from exc

        raw = await process.stdout.readline()
        if not raw:
            await self._terminate("读到 EOF")
            raise EmbedProcessError(f"嵌入子进程提前退出，未返回响应{self._stderr_hint()}")

        try:
            response = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise EmbedProcessError(f"嵌入子进程返回的不是 JSON：{raw[:200]!r}") from exc

        if response.get("id") != request_id:
            raise EmbedProcessError(
                f"响应 id 不匹配：期望 {request_id}，实际 {response.get('id')!r}"
            )
        return response

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        """确保子进程已就绪；惰性启动且只起一个。

        Returns:
            可用的子进程句柄。

        Raises:
            EmbedProcessError: 解释器或脚本缺失，或启动/就绪握手失败。
        """
        process = self._process
        if process is not None and process.returncode is None:
            return process

        async with self._spawn_lock:
            # 双检：并发首次调用时，等锁期间可能已被另一个协程启动
            process = self._process
            if process is not None and process.returncode is None:
                return process

            if not self.python.exists():
                raise EmbedProcessError(
                    f"未找到嵌入运行时的解释器：{self.python}。"
                    "先执行 python scripts/setup_embed_venv.py 准备独立环境"
                )
            if not self.server.is_file():
                raise EmbedProcessError(f"未找到嵌入服务脚本：{self.server}")

            logger.info("启动嵌入子进程：%s %s --model %s", self.python, self.server.name, self.model)
            self._stderr_tail.clear()
            process = await asyncio.create_subprocess_exec(
                str(self.python),
                str(self.server),
                "--model",
                self.model,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._process = process
            self._stderr_task = asyncio.create_task(self._drain_stderr(process))

            try:
                await asyncio.wait_for(self._wait_ready(process), timeout=_READY_TIMEOUT_SECONDS)
            except BaseException:
                # 含 CancelledError：被取消时若不收拾，会留下一个占着内存的孤儿进程
                await self._terminate("启动未完成")
                raise
            return process

    async def _wait_ready(self, process: asyncio.subprocess.Process) -> None:
        """消费子进程的 ready 行。

        Raises:
            EmbedProcessError: 读到 EOF（进程未就绪即退出）或响应形状不对。
        """
        if process.stdout is None:
            raise EmbedProcessError("嵌入子进程的 stdout 不可用")

        raw = await process.stdout.readline()
        if not raw:
            raise EmbedProcessError(f"嵌入子进程启动后立即退出{self._stderr_hint()}")

        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise EmbedProcessError(f"就绪握手返回的不是 JSON：{raw[:200]!r}") from exc

        # WHY 单独识别 error 事件：子进程在**模型加载**阶段失败时会这样报告（它自己
        # 知道原因，而父进程只能看到 EOF）。直接把它的原文抛出来，用户才能看见
        # 「缺 fastembed」这类真正的原因，而不是一句笼统的握手失败。
        if payload.get("event") == "error":
            raise EmbedProcessError(f"嵌入子进程加载模型失败：{payload.get('error')}")

        if payload.get("event") != "ready":
            raise EmbedProcessError(f"就绪握手异常：{str(payload)[:200]}")

        logger.info(
            "嵌入子进程就绪：model=%s 加载 %.2fs 常驻 %s MB",
            payload.get("model"),
            float(payload.get("load_seconds") or 0.0),
            payload.get("rss_mb"),
        )

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        """持续读走 stderr 并保留末尾若干行。

        WHY 必须有人读：stderr 管道写满后子进程会阻塞在写操作上，表现为「推理莫名
        卡死」。WHY 保留末几行：独立的模型环境最常见的失败是缺依赖（``fastembed``
        没装、onnxruntime 轮子不匹配），而它的报错只在 stderr 上——不留下这些行，
        用户看到的就只是一句「子进程提前退出」。
        """
        if process.stderr is None:
            return
        try:
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
                    logger.debug("嵌入子进程 stderr: %s", line)
        except asyncio.CancelledError:  # pragma: no cover - 仅随事件循环关闭发生
            return

    def _stderr_hint(self) -> str:
        """把子进程 stderr 的末几行拼成一段可读的诊断后缀。"""
        if not self._stderr_tail:
            return ""
        tail = " / ".join(self._stderr_tail)
        return f"（子进程 stderr：{tail[-500:]}）"

    def _schedule_idle_shutdown(self) -> None:
        """安排空闲回收；``idle_seconds`` 为 0 时不安排。"""
        if self._idle_seconds <= 0:
            return
        self._cancel_idle_shutdown()
        self._idle_task = asyncio.create_task(self._idle_shutdown())

    def _cancel_idle_shutdown(self) -> None:
        """取消待执行的空闲回收任务。"""
        task = self._idle_task
        self._idle_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _idle_shutdown(self) -> None:
        """空闲到期后回收子进程。"""
        try:
            await asyncio.sleep(self._idle_seconds)
        except asyncio.CancelledError:
            return

        # WHY 再查一次锁：本任务被取消的时机可能刚好与新请求擦肩而过，此时收掉进程
        # 会让正在进行的请求读到 EOF。
        if self._request_lock.locked():
            return
        await self._terminate(f"空闲超过 {self._idle_seconds}s")

    async def _terminate(self, reason: str) -> None:
        """终止子进程并清空引用；幂等，可在任意失败路径上调用。"""
        self._cancel_idle_shutdown()

        process = self._process
        self._process = None
        stderr_task = self._stderr_task
        self._stderr_task = None

        if process is not None and process.returncode is None:
            logger.info("终止嵌入子进程：%s", reason)
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
            except (ProcessLookupError, asyncio.TimeoutError):
                # 宽限期内未退出（或已消失）都要落到 kill：留下一个占着 189 MB 的
                # 孤儿进程，比多杀一次更糟。
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass

        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()


def default_embed_python(data_dir: Path) -> Path:
    """返回约定位置下嵌入环境的解释器路径。

    WHY 由 ``data_dir``（即 ``db_path`` 的父目录）推导而不是写死 ``.data``：数据目录
    是可配置的，而模型环境与数据目录同处一块可写卷上，容器部署时才不会出现「数据卷
    挂上了、模型却写到了镜像层」这种把 189 MB 落进只读层的问题。

    Args:
        data_dir: 应用数据目录。

    Returns:
        约定的解释器路径（不保证存在，存在性由 ``EmbedProcessClient`` 在启动前检查）。
    """
    venv = Path(data_dir) / "embed-venv"
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"
