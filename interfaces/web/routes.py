"""REST 与 SSE 路由。

约定：
- 一次性操作走 REST；
- 会产生持续输出的操作（执行、恢复）走 SSE，由服务端逐帧推送统一事件，
  前端不需要理解 LangGraph 的内部结构。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from application.dto import ModelInfo
from application.errors import ThreadBusyError
from application.events import AgentEvent
from application.model_catalog import ModelCatalog
from application.run_service import RunService
from application.thread_service import ThreadService
from interfaces.web.schemas import (
    ChatRequest,
    DeleteResponse,
    HistoryMessage,
    ResumeRequest,
    ThreadListResponse,
    ThreadResponse,
)
from interfaces.web.sse import SSE_HEADERS, encode_sse
from runtime.thread_store import normalize_thread_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ 依赖注入


def get_threads(request: Request) -> ThreadService:
    """取出会话服务单例。

    WHY 单例：图与 checkpointer 都是有状态的重量对象，每个请求新建会导致
    连接池耗尽，也会让同一 thread 在并发请求中读到不一致的状态。
    """
    return _require_state(request, "threads", "会话服务")


def get_runs(request: Request) -> RunService:
    """取出运行服务单例。"""
    return _require_state(request, "runs", "运行服务")


def get_catalog(request: Request) -> ModelCatalog:
    """取出模型目录单例。"""
    return _require_state(request, "catalog", "模型目录")


def _require_state(request: Request, attr: str, label: str) -> Any:
    """从应用状态取服务，缺失时返回 500。"""
    service = getattr(request.app.state, attr, None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{label}未初始化",
        )
    return service


def _validate_thread_id(thread_id: str) -> str:
    """校验路径参数中的会话 ID。

    WHY 复用存储层的 ``normalize_thread_id``：会话 ID 的合法性规则只有一份定义，
    路由层只负责把 ``ValueError`` 翻译成 400，不再自己维护一套判断。
    """
    try:
        return normalize_thread_id(thread_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


# ------------------------------------------------------------------ 路由


@router.get("/models", response_model=list[ModelInfo])
async def list_models(catalog: ModelCatalog = Depends(get_catalog)) -> list[ModelInfo]:
    """列出可切换的模型。"""
    return catalog.list_models()


@router.post("/threads", response_model=ThreadResponse)
async def create_thread(threads: ThreadService = Depends(get_threads)) -> ThreadResponse:
    """申请一个新的会话 ID。

    WHY 不是 201 Created：此刻并没有创建任何资源——会话要等首条消息被接受
    （``/runs``）才真正诞生并在数据库中留下记录，返回 200 才是诚实的语义。
    """
    return ThreadResponse(thread_id=threads.new_thread_id())


@router.get("/threads", response_model=ThreadListResponse)
async def list_threads(
    limit: int = Query(default=50, ge=1, le=200, description="返回条数"),
    offset: int = Query(default=0, ge=0, description="跳过的条数"),
    threads: ThreadService = Depends(get_threads),
) -> ThreadListResponse:
    """列出会话清单，最近活动的在前。

    WHY 注册在 ``/threads/{thread_id}`` 之前：路径段数不同本不冲突，但把集合
    资源放在单资源之前可以让路由表读起来与 REST 语义一致，避免以后新增
    ``/threads/summary`` 之类的静态子路径时被参数路由抢先匹配。
    """
    try:
        result = await threads.list_threads(limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return ThreadListResponse(items=result.items, total=result.total)


@router.get("/threads/{thread_id}", response_model=list[HistoryMessage])
async def get_history(
    thread_id: str,
    threads: ThreadService = Depends(get_threads),
) -> list[HistoryMessage]:
    """读取会话历史，用于刷新页面后恢复上下文。"""
    normalized = _validate_thread_id(thread_id)
    try:
        return await threads.history(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except RuntimeError as exc:
        logger.exception("读取会话历史失败：thread=%s", normalized)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.delete("/threads/{thread_id}", response_model=DeleteResponse)
async def delete_thread(
    thread_id: str,
    threads: ThreadService = Depends(get_threads),
) -> DeleteResponse:
    """删除会话。"""
    normalized = _validate_thread_id(thread_id)
    try:
        result = await threads.delete_thread(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return DeleteResponse(
        thread_id=result.thread_id,
        deleted=result.deleted,
        outcome=result.outcome,
        detail=result.detail,
    )


@router.post("/threads/{thread_id}/runs")
async def run_agent(
    thread_id: str,
    body: ChatRequest,
    runs: RunService = Depends(get_runs),
) -> StreamingResponse:
    """发起一轮对话，以 SSE 流式返回事件。

    WHY 不再需要单独的就绪检查：``RunService.stream`` 是普通协程，参数校验与
    模型初始化都在 ``await`` 时同步完成，因此错误能在响应开始之前被映射成
    正常的状态码，不必再为一个 SSE 的传输限制而在服务层额外开一个 API。
    """
    normalized = _validate_thread_id(thread_id)

    try:
        events = await runs.stream(normalized, body.content, model_name=body.model)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"未知模型：{exc}"
        ) from exc
    except ThreadBusyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except RuntimeError as exc:
        logger.exception("Agent 初始化失败：thread=%s", normalized)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent 初始化失败：{exc}",
        ) from exc

    return StreamingResponse(
        _encode_stream(events),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/threads/{thread_id}/resume")
async def resume_agent(
    thread_id: str,
    body: ResumeRequest,
    runs: RunService = Depends(get_runs),
) -> StreamingResponse:
    """人工审批后恢复执行，同样以 SSE 流式返回。"""
    normalized = _validate_thread_id(thread_id)

    payload = {
        "decisions": [item.model_dump(exclude_none=True) for item in body.decisions]
    }

    try:
        events = await runs.resume(normalized, payload, model_name=body.model)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"未知模型：{exc}"
        ) from exc
    except ThreadBusyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except RuntimeError as exc:
        logger.exception("Agent 初始化失败：thread=%s", normalized)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent 初始化失败：{exc}",
        ) from exc

    return StreamingResponse(
        _encode_stream(events),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _encode_stream(events: AsyncIterator[AgentEvent]) -> AsyncIterator[str]:
    """把应用事件流转成 SSE 文本帧流。"""
    async for event in events:
        yield encode_sse(event)
