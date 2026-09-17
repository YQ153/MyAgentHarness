"""REST 与 SSE 路由。

约定：
- 一次性操作走 REST；
- 会产生持续输出的操作（执行、恢复）走 SSE，由服务端逐帧推送统一事件，
  前端不需要理解 LangGraph 的内部结构。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from application.dto import (
    MemoryDeleteResult,
    MemoryListResult,
    ModelInfo,
    ToolListResult,
    UsageSummary,
)
from application.errors import (
    InterruptExpiredError,
    NotFoundError,
    OwnershipError,
    PermissionDeniedError,
    ThreadBusyError,
)
from application.events import AgentEvent
from application.memory_service import MemoryService
from application.model_catalog import ModelCatalog
from application.principal import Principal
from application.run_service import RunService
from application.thread_id import normalize_thread_id
from application.thread_service import ThreadService
from application.tool_catalog import ToolCatalog
from application.usage_service import UsageService
from interfaces.web.auth import get_principal, require_permission
from interfaces.web.deps import require_state
from interfaces.web.schemas import (
    ChatRequest,
    DeleteResponse,
    HistoryMessage,
    ResumeRequest,
    StopResponse,
    ThreadListResponse,
    ThreadResponse,
)
from interfaces.web.sse import SSE_HEADERS, encode_sse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ 依赖注入


def get_threads(request: Request) -> ThreadService:
    """取出会话服务单例。

    WHY 单例：图与 checkpointer 都是有状态的重量对象，每个请求新建会导致
    连接池耗尽，也会让同一 thread 在并发请求中读到不一致的状态。
    """
    return require_state(request, "threads", "会话服务")


def get_runs(request: Request) -> RunService:
    """取出运行服务单例。"""
    return require_state(request, "runs", "运行服务")


def get_catalog(request: Request) -> ModelCatalog:
    """取出模型目录单例。"""
    return require_state(request, "catalog", "模型目录")


def get_usage(request: Request) -> UsageService:
    """取出用量统计服务单例。"""
    return require_state(request, "usage", "用量统计服务")


def get_tool_catalog(request: Request) -> ToolCatalog:
    """取出工具目录单例。"""
    return require_state(request, "tools", "工具目录")


def get_memory_service(request: Request) -> MemoryService:
    """取出记忆管理服务单例。"""
    return require_state(request, "memories", "记忆管理服务")


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


@router.get("/tools", response_model=ToolListResult)
async def list_tools(
    catalog: ToolCatalog = Depends(get_tool_catalog),
    principal: Principal = Depends(require_permission("tool:read")),
) -> ToolListResult:
    """列出当前生效的工具及其来源。

    WHY 需要这个端点：MCP 服务器是在启动期静态加载的，加载失败时既没有
    请求报错也没有界面提示——用户只会发现「助手不会做某件事」。把它做成
    可查询的状态，才能让「装了但没生效」这类问题在首次排查时就被看见。

    WHY 走 ``tool:read`` 而不是 ``system:models``：模型清单是「可选配置」，
    工具清单是「实际具备的能力」，两者的变更来源与排查路径都不同。
    """
    return catalog.list_tools()


@router.get("/usage", response_model=UsageSummary)
async def get_usage_summary(
    thread_id: str | None = Query(default=None, description="限定会话；不传表示当前主体可见的全部"),
    days: int | None = Query(default=None, ge=1, description="统计窗口天数；不传取配置默认值"),
    group_by: str = Query(default="model", description="聚合维度：model / thread / day"),
    usage: UsageService = Depends(get_usage),
    principal: Principal = Depends(require_permission("usage:read")),
) -> UsageSummary:
    """按用户 / 会话 / 时间窗汇总 token 用量。

    WHY 走 ``usage:read`` 而不是复用 ``thread:read``：用量是跨会话的成本数据，
    能读某条会话不等于能看整体开销；独立权限才能在只读角色上精确收口。

    WHY 非管理员即使有权限也只看到自己的：服务层按 ``owner_id`` 收敛数据，
    权限控制的是「能不能调这个接口」，不是「能看到谁的数据」。
    """
    try:
        return await usage.summarize(
            principal,
            thread_id=thread_id,
            days=days,
            group_by=group_by,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except OwnershipError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("用量聚合失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.get("/memories", response_model=MemoryListResult)
async def list_memories(
    memories: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(require_permission("memory:read")),
) -> MemoryListResult:
    """列出当前主体自己的长期记忆。

    WHY 不做「管理员查看他人记忆」：记忆是个人数据（偏好、项目约定），跨主体
    读取需要独立的授权与审计设计；管理员在本端点同样只看自己的那一份，否则
    这个接口会变成一条绕过会话归属的旁路。
    """
    try:
        return await memories.list_memories(principal)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("读取长期记忆失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.delete("/memories/{path:path}", response_model=MemoryDeleteResult)
async def delete_memory(
    path: str,
    memories: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(require_permission("memory:delete")),
) -> MemoryDeleteResult:
    """删除一条长期记忆。

    WHY 用 ``{path:path}``：记忆路径自身含 ``/``（``/memories/notes.md``），
    普通路径参数会在第一个分隔符处截断。路径以 ``notes.md``（相对挂载点）或
    ``memories/notes.md``（完整虚拟路径，即 GET 返回值）传入均可，服务层统一
    归一——前端直接把清单里的 path 拼在端点后面即可。

    WHY 删除不存在的条目仍返回 200：这是一次幂等的「忘掉它」，界面刷新后
    重放请求是正常行为；返回 404 只会让用户看到一条与真实结果无关的报错。
    """
    try:
        return await memories.delete_memory(path, principal)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("删除长期记忆失败：path=%s", path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.post("/threads", response_model=ThreadResponse)
async def create_thread(
    threads: ThreadService = Depends(get_threads),
    principal: Principal = Depends(require_permission("thread:create")),
) -> ThreadResponse:
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
    principal: Principal = Depends(require_permission("thread:list")),
) -> ThreadListResponse:
    """列出会话清单，最近活动的在前。

    WHY 注册在 ``/threads/{thread_id}`` 之前：路径段数不同本不冲突，但把集合
    资源放在单资源之前可以让路由表读起来与 REST 语义一致，避免以后新增
    ``/threads/summary`` 之类的静态子路径时被参数路由抢先匹配。
    """
    try:
        result = await threads.list_threads(principal, limit=limit, offset=offset)
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
    principal: Principal = Depends(require_permission("thread:read")),
) -> list[HistoryMessage]:
    """读取会话历史，用于刷新页面后恢复上下文。"""
    normalized = _validate_thread_id(thread_id)
    try:
        return await threads.history(normalized, principal)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except OwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
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
    runs: RunService = Depends(get_runs),
    principal: Principal = Depends(require_permission("thread:delete")),
) -> DeleteResponse:
    """删除会话。"""
    normalized = _validate_thread_id(thread_id)
    try:
        result = await threads.delete_thread(normalized, principal)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except OwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc

    # WHY 同步清理挂起审批登记：会话已被删除，若留着那条登记，「待审批数」
    # 会永久多算一个永不存在的会话——指标一旦失真就没人再信它。
    runs.clear_hitl_pending(normalized)

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
    principal: Principal = Depends(require_permission("thread:create")),
) -> StreamingResponse:
    """发起一轮对话，以 SSE 流式返回事件。

    WHY 不再需要单独的就绪检查：``RunService.stream`` 是普通协程，参数校验与
    模型初始化都在 ``await`` 时同步完成，因此错误能在响应开始之前被映射成
    正常的状态码，不必再为一个 SSE 的传输限制而在服务层额外开一个 API。
    """
    normalized = _validate_thread_id(thread_id)

    try:
        events = await runs.stream(normalized, body.content, principal=principal, model_name=body.model)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"未知模型：{exc}"
        ) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except OwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
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
    principal: Principal = Depends(require_permission("hitl:approve")),
) -> StreamingResponse:
    """人工审批后恢复执行，同样以 SSE 流式返回。

    WHY 用 ``hitl:approve`` 而不是 ``thread:create``：审批会让此前被拦下的
    高危工具真正执行，风险量级高于发起对话，必须与「能发消息」解耦。
    """
    normalized = _validate_thread_id(thread_id)

    payload = {
        "decisions": [item.model_dump(exclude_none=True) for item in body.decisions]
    }

    try:
        events = await runs.resume(normalized, payload, principal=principal, model_name=body.model)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"未知模型：{exc}"
        ) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except OwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except ThreadBusyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except InterruptExpiredError as exc:
        # WHY 必须排在 RuntimeError 之前：它是 RuntimeError 的子类，顺序颠倒
        # 时「审批已过期」会被当成未捕获的服务异常回成 500，用户看到的就不是
        # 「请重新发起」而是「服务器内部错误」。
        logger.info("拒绝已过期的审批：thread=%s", normalized)
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


@router.post("/threads/{thread_id}/stop", response_model=StopResponse)
async def stop_run(
    thread_id: str,
    runs: RunService = Depends(get_runs),
    principal: Principal = Depends(require_permission("thread:create")),
) -> StopResponse:
    """请求停止会话的当前运行。

    WHY 幂等返回 200 而不是 409：停止请求的意图是「让运行停下来」，
    会话未在运行时该意图视为已满足；409 暗示冲突，会把「连点停止按钮」
    变成一次报错。真正的鉴权失败（403 / 404）仍然照常返回。
    """
    normalized = _validate_thread_id(thread_id)

    try:
        result = await runs.stop(normalized, principal=principal)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except OwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc

    return StopResponse(**result)


async def _encode_stream(events: AsyncIterator[AgentEvent]) -> AsyncIterator[str]:
    """把应用事件流转成 SSE 文本帧流。"""
    async for event in events:
        yield encode_sse(event)
