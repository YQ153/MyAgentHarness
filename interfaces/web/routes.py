"""REST 与 SSE 路由。

约定：
- 一次性操作走 REST；
- 会产生持续输出的操作（执行、恢复）走 SSE，由服务端逐帧推送统一事件，
  前端不需要理解 LangGraph 的内部结构。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from application.agent_service import AgentService
from interfaces.web.schemas import (
    ChatRequest,
    DeleteResponse,
    HistoryMessage,
    ModelInfo,
    ResumeRequest,
    ThreadListResponse,
    ThreadResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

_SSE_HEADERS: dict[str, str] = {
    # WHY no-transform 与 X-Accel-Buffering：反向代理默认会缓冲响应，
    # 会让流式输出退化成一次性返回，这两个头是关掉缓冲的标准做法。
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def get_service(request: Request) -> AgentService:
    """从应用状态取出会话服务单例。

    WHY 单例：图与 checkpointer 都是有状态的重量对象，每个请求新建会导致
    连接池耗尽，也会让同一 thread 在并发请求中读到不一致的状态。
    """
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="会话服务未初始化",
        )
    return service


def _validate_thread_id(thread_id: str) -> str:
    if not thread_id or not thread_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="thread_id 不能为空",
        )
    return thread_id.strip()


@router.get("/models", response_model=list[ModelInfo])
async def list_models(service: AgentService = Depends(get_service)) -> list[dict[str, str]]:
    """列出可切换的模型。"""
    return service.models()


@router.post("/threads", response_model=ThreadResponse)
async def create_thread(service: AgentService = Depends(get_service)) -> ThreadResponse:
    """申请一个新的会话 ID。

    WHY 不是 201 Created：此刻并没有创建任何资源——会话要等首条消息被接受
    （``/runs``）才真正诞生并在数据库中留下记录，返回 200 才是诚实的语义。
    """
    return ThreadResponse(thread_id=service.new_thread())


@router.get("/threads", response_model=ThreadListResponse)
async def list_threads(
    limit: int = Query(default=50, ge=1, le=200, description="返回条数"),
    offset: int = Query(default=0, ge=0, description="跳过的条数"),
    service: AgentService = Depends(get_service),
) -> ThreadListResponse:
    """列出会话清单，最近活动的在前。

    WHY 注册在 ``/threads/{thread_id}`` 之前：路径段数不同本不冲突，但把集合
    资源放在单资源之前可以让路由表读起来与 REST 语义一致，避免以后新增
    ``/threads/summary`` 之类的静态子路径时被参数路由抢先匹配。
    """
    try:
        payload = await service.list_threads(limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return ThreadListResponse(**payload)


@router.get("/threads/{thread_id}", response_model=list[HistoryMessage])
async def get_history(
    thread_id: str,
    service: AgentService = Depends(get_service),
) -> list[dict[str, Any]]:
    """读取会话历史，用于刷新页面后恢复上下文。"""
    _validate_thread_id(thread_id)
    return await service.history(thread_id)


@router.delete("/threads/{thread_id}", response_model=DeleteResponse)
async def delete_thread(
    thread_id: str,
    service: AgentService = Depends(get_service),
) -> DeleteResponse:
    """删除会话。"""
    _validate_thread_id(thread_id)
    deleted = await service.delete_thread(thread_id)
    return DeleteResponse(thread_id=thread_id, deleted=deleted)


def _ensure_ready(service: AgentService, model_name: str | None) -> None:
    """在开启 SSE 之前完成模型与图的构造。

    WHY 放在这里而不是依赖 StreamingResponse 内部的异常：流式响应一旦开始，
    状态码就无法再改，错误只能以「连接断开」的形式暴露给前端。
    """
    try:
        service.ensure_ready(model_name)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"未知模型：{exc}",
        ) from exc
    except Exception as exc:
        logger.exception("Agent 就绪检查失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent 初始化失败：{exc}",
        ) from exc


@router.post("/threads/{thread_id}/runs")
async def run_agent(
    thread_id: str,
    body: ChatRequest,
    service: AgentService = Depends(get_service),
) -> StreamingResponse:
    """发起一轮对话，以 SSE 流式返回事件。"""
    _validate_thread_id(thread_id)
    _ensure_ready(service, body.model)

    async def generate() -> Any:
        async for event in service.stream(
            thread_id,
            body.content,
            model_name=body.model,
        ):
            yield event.encode()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/threads/{thread_id}/resume")
async def resume_agent(
    thread_id: str,
    body: ResumeRequest,
    service: AgentService = Depends(get_service),
) -> StreamingResponse:
    """人工审批后恢复执行，同样以 SSE 流式返回。"""
    _validate_thread_id(thread_id)
    _ensure_ready(service, body.model)

    payload = {"decisions": [item.model_dump(exclude_none=True) for item in body.decisions]}

    async def generate() -> Any:
        async for event in service.resume(
            thread_id,
            payload,
            model_name=body.model,
        ):
            yield event.encode()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
