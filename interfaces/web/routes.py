"""REST 与 SSE 路由。

约定：
- 一次性操作走 REST；
- 会产生持续输出的操作（执行、恢复）走 SSE，由服务端逐帧推送统一事件，
  前端不需要理解 LangGraph 的内部结构。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from application.dto import (
    BranchListResult,
    DirectoryListing,
    ImportResult,
    MemoryDeleteResult,
    MemoryListResult,
    ModelInfo,
    ThreadExport,
    ThreadSummary,
    ToolListResult,
    UsageSummary,
    WorkspacePickResult,
)
from application.errors import (
    InterruptExpiredError,
    NotFoundError,
    OwnershipError,
    PermissionDeniedError,
    RunRejectedError,
    SessionRootLockedError,
    SessionRootNotReadyError,
    SessionRootUnavailableError,
    ThreadBusyError,
    VisionUnsupportedError,
)
from application.events import AgentEvent
from application.memory_service import MemoryService
from application.model_catalog import ModelCatalog
from application.principal import Principal
from application.run_service import RunService
from application.session_registry import (
    FolderPickerBusyError,
    FolderPickerTimeoutError,
    FolderPickerUnavailableError,
)
from application.thread_export import render_markdown
from application.thread_service import ThreadService
from application.tool_catalog import ToolCatalog
from application.usage_service import UsageService
from interfaces.web.auth import get_principal, require_permission
from interfaces.web.deps import get_session_registry, require_state, resolve_scoped_services
from interfaces.web.schemas import (
    ChatRequest,
    DeleteResponse,
    EditRequest,
    HistoryMessage,
    RegenerateRequest,
    ResumeRequest,
    StopResponse,
    ThreadListResponse,
    ThreadResponse,
    ThreadUpdateRequest,
)
from interfaces.web.sse import SSE_HEADERS, encode_sse
from thread_utils import normalize_thread_id

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

    WHY 复用中立模块的 ``normalize_thread_id``：会话 ID 的合法性规则只有一份定义，
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


@router.get("/workspaces/dirs", response_model=DirectoryListing)
async def list_workspace_dirs(
    path: str | None = Query(
        default=None, description="要列的目录；不传则返回起点（盘符或 /）"
    ),
    registry: Any = Depends(get_session_registry),
    principal: Principal = Depends(require_permission("file:read")),
) -> DirectoryListing:
    """列出一个目录下的子目录，供界面逐级挑选工作空间。

    WHY 没有边界：工作空间允许用户任意选择（这是产品规则），因此服务端不过滤位置——
    能选任意目录就意味着能读任意目录，这里的门槛只剩权限本身。

    只列目录、只列一层：这一步的用途是挑目录；一次列全整棵树会让响应变成一次全盘扫描。

    WHY 用 ``asyncio.to_thread``：``iterdir`` 是阻塞的系统调用，在事件循环里直接跑会让
    一次慢盘列举拖住所有并发请求（与文件面板同一口径，见 ``WorkspaceService``）。

    权限与文件面板同口径（``file:read``）：它暴露的是宿主机上的绝对路径。
    """
    try:
        return await asyncio.to_thread(registry.list_directories, path)
    except ValueError as exc:
        # 路径不存在 / 不是目录：改路径就能过，是 400 而不是 403。
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/workspaces/pick", response_model=WorkspacePickResult)
async def pick_workspace(
    workspace: str | None = Query(default=None, description="对话框的起始目录；不传则用服务端的主目录"),
    registry: Any = Depends(get_session_registry),
    principal: Principal = Depends(require_permission("file:read")),
) -> WorkspacePickResult:
    """在**服务端**弹出系统文件夹选择对话框，把选中的路径回给浏览器。

    WHY 需要它：浏览器页面拿不到宿主的绝对路径——``<input webkitdirectory>`` 只给相对名，
    File System Access API 只给一个 handle。要拿到 ``D:\\projects\\my-app`` 这种取值，
    只能由服务端进程在自己的桌面上弹原生对话框。

    代价是明确的：这个对话框出现在**服务端那台机器**的屏幕上，而不是访问浏览器的人眼前。
    因此它只适用于「服务端就跑在你自己机器上」这种形态；容器、无显示器的服务器、以及
    服务端与浏览器分离的部署都会得到 501，那时应当用 ``/workspaces/dirs`` 逐级挑选。

    WHY 是 POST：它会在宿主上弹出一个窗口，属于有副作用的动作。做成 GET 会被浏览器
    预取、被中间层缓存，凭空多出几个没人认领的弹窗。

    WHY ``asyncio.to_thread``：这一步要等用户关上对话框（最长见
    ``folder_picker.DEFAULT_TIMEOUT_SECONDS``），阻塞在事件循环里会让整个服务停摆。
    """
    try:
        chosen = await asyncio.to_thread(registry.pick_folder, workspace)
    except FolderPickerUnavailableError as exc:
        # 501 而不是 500：这不是「服务出错了」，而是「这个部署形态没有图形环境」——
        # 客户端据此该做的事是换一条路径（网页内浏览），而不是重试。
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
        ) from exc
    except FolderPickerBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except FolderPickerTimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if chosen is None:
        return WorkspacePickResult(cancelled=True)
    return WorkspacePickResult(cancelled=False, path=chosen)


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
    query: str | None = Query(default=None, description="标题关键字；不传表示不过滤"),
    tag: str | None = Query(default=None, description="按标签过滤；不传表示不过滤"),
    include_archived: bool = Query(default=False, description="是否包含已归档的会话"),
    threads: ThreadService = Depends(get_threads),
    principal: Principal = Depends(require_permission("thread:list")),
) -> ThreadListResponse:
    """列出会话清单，最近活动的在前。

    WHY 注册在 ``/threads/{thread_id}`` 之前：路径段数不同本不冲突，但把集合
    资源放在单资源之前可以让路由表读起来与 REST 语义一致，避免以后新增
    ``/threads/summary`` 之类的静态子路径时被参数路由抢先匹配。

    WHY 搜索只匹配标题：正文存在检查点的 msgpack BLOB 里，逐条反序列化检索的
    成本与「列一张窄表」完全不是一个量级；把这条边界写在接口上，比让它表现成
    「搜索偶尔很慢」要诚实。
    """
    try:
        result = await threads.list_threads(
            principal,
            limit=limit,
            offset=offset,
            query=query,
            tag=tag,
            include_archived=include_archived,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return ThreadListResponse(items=result.items, total=result.total)


@router.patch("/threads/{thread_id}", response_model=ThreadSummary)
async def update_thread(
    thread_id: str,
    body: ThreadUpdateRequest,
    threads: ThreadService = Depends(get_threads),
    principal: Principal = Depends(require_permission("thread:update")),
) -> ThreadSummary:
    """重命名或归档会话。

    WHY 用 ``thread:update`` 而不是复用 ``thread:delete``：归档可逆、改名无破坏性，
    与「把数据删掉」不是同一风险量级；复用一个权限会让只想授予整理能力的角色
    连带拿到删除权。
    """
    normalized = _validate_thread_id(thread_id)
    if body.title is None and body.archived is None and body.tags is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="title / archived / tags 至少要提供一项",
        )

    try:
        # WHY 三项各自独立判定，而不是 if / elif 串起来：一次请求可以同时改名与打标签，
        # 它们互不依赖；用 else 串联会让「只传 tags」的请求走进归档分支，把
        # ``bool(None)`` 当成 False 顺手把会话取消归档——用户只想加个标签，结果会话
        # 从归档里冒了出来。
        if body.title is not None:
            result = await threads.rename_thread(normalized, body.title, principal)
        if body.archived is not None:
            result = await threads.set_archived(normalized, bool(body.archived), principal)
        if body.tags is not None:
            result = await threads.update_tags(normalized, body.tags, principal)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except OwnershipError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("更新会话失败：thread=%s", normalized)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return result


@router.get("/threads/{thread_id}/export")
async def export_thread(
    thread_id: str,
    format: str = Query(
        default="json", pattern="^(json|markdown)$", description="导出格式"
    ),
    branch: str | None = Query(default=None, description="要导出的分支；缺省为当前分支"),
    threads: ThreadService = Depends(get_threads),
    principal: Principal = Depends(require_permission("thread:read")),
) -> Response:
    """导出会话：JSON 供机器读，Markdown 供人读。

    WHY 用 ``Response`` 而不是 ``response_model``：两种格式的内容类型不同，且都以
    「文件」形式交付——带上 ``Content-Disposition`` 才能让浏览器下载而不是内联展示。
    """
    normalized = _validate_thread_id(thread_id)

    try:
        payload = await threads.export_thread(normalized, principal, branch_id=branch)
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

    if format == "markdown":
        body, media_type, suffix = render_markdown(payload), "text/markdown", "md"
    else:
        body, media_type, suffix = payload.model_dump_json(indent=2), "application/json", "json"

    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="thread-{normalized[:12]}.{suffix}"'
        },
    )


@router.post("/threads/import", response_model=ImportResult)
async def import_thread(
    body: ThreadExport,
    workspace: str | None = Query(
        default=None,
        description="导入后的新会话使用哪个工作空间；缺省表示不绑定，用它的会话专属目录",
    ),
    threads: ThreadService = Depends(get_threads),
    principal: Principal = Depends(require_permission("thread:create")),
) -> ImportResult:
    """把导出的 JSON 复原成一个**新会话**。

    WHY 请求体就是导出文件本身：这样「导出 → 导入」是一条无转换的路径，不需要再
    约定一层包装格式——多一层包装就多一处可能对不上的字段名。工作空间因此只能走查询
    参数（它是**导入方**的决定，不是文件里的内容）。

    WHY 不照搬文件里的 ``workspace``：那是来源机器上的绝对路径，在本机通常不存在，
    照搬会让导入直接失败。文件里的取值只作为线索（也在 ``notes`` 里说明）。
    """
    try:
        return await threads.import_thread(body, principal, workspace=workspace)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except RuntimeError as exc:
        logger.exception("导入会话失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.get("/threads/{thread_id}", response_model=list[HistoryMessage])
async def get_history(
    thread_id: str,
    branch: str | None = Query(default=None, description="要读取的分支；缺省为当前分支"),
    threads: ThreadService = Depends(get_threads),
    principal: Principal = Depends(require_permission("thread:read")),
) -> list[HistoryMessage]:
    """读取会话历史，用于刷新页面后恢复上下文。

    WHY 用查询参数而不是路径段指定分支：根分支的标识是空串，而路径上无法表达空值。
    """
    normalized = _validate_thread_id(thread_id)
    try:
        return await threads.history(normalized, principal, branch_id=branch)
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
    except (SessionRootNotReadyError, SessionRootUnavailableError) as exc:
        # WHY 单独接：读历史要按会话的根装配图与附件索引（历史里的图片引用是相对本根的
        # 路径），因此根没就绪或目录不见了都会在这里失败。两者都是 409：用户能做的事
        # 是明确的（先发出第一条消息 / 把目录恢复回来），而不是「服务端故障，请稍后再试」。
        # 兜到下面那条分支会得到 500 + 一句内部断言，与用户的操作毫无关系。
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
    request: Request,
    runs: RunService = Depends(get_runs),
    principal: Principal = Depends(require_permission("thread:create")),
) -> StreamingResponse:
    """发起一轮对话，以 SSE 流式返回事件。

    WHY 不再需要单独的就绪检查：``RunService.stream`` 是普通协程，参数校验与
    模型初始化都在 ``await`` 时同步完成，因此错误能在响应开始之前被映射成
    正常的状态码，不必再为一个 SSE 的传输限制而在服务层额外开一个 API。

    带上 ``attachment_ids`` 时，消息会先被构造成多模态内容块；若目标模型不接受
    图片，这里直接返回 400 并说明可用的多模态模型，**不会**把图片静默丢掉。
    """
    normalized = _validate_thread_id(thread_id)

    # WHY 附件服务在这里现取、而不是走 ``get_attachments`` 依赖：附件必须与运行落在
    # **同一个**工作区，而本次请求要用的工作区在 body 里（不是查询参数）。走依赖会读到
    # 另一个值——新会话的附件当场「不存在」，而它明明刚上传成功。
    attachments = (
        await resolve_scoped_services(request, thread_id=normalized, requested=body.workspace)
    ).attachments

    try:
        content: str | list[dict[str, Any]] = body.content
        if body.attachment_ids:
            content = await attachments.build_user_content(
                normalized,
                body.content,
                body.attachment_ids,
                model_name=body.model,
                principal=principal,
            )
        events = await runs.stream(
            normalized,
            content,
            principal=principal,
            model_name=body.model,
            workspace=body.workspace,
        )
    except (SessionRootLockedError, SessionRootNotReadyError, SessionRootUnavailableError) as exc:
        # WHY 409：三种都是「状态不允许这次操作」——已锁定（这条会话的根定了）、还没
        # 就绪（这条会话还没有根）、不可用（根目录不见了）。重试本请求无用，客户端应改用
        # 原根、新建会话，或把那个目录恢复回来。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except VisionUnsupportedError as exc:
        # WHY 单独先接：它是 ValueError 的子类，落到下面那条分支就只会得到
        # 一句裸错误文本，而这里要保证「换哪个模型」这个关键信息一定被回出去。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
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
    except RunRejectedError as exc:
        # WHY 必须带 Retry-After：429 只说「太多了」，客户端仍不知道何时可重试，
        # 于是只能盲猜间隔——那等于把一次明确的服务端决策变成客户端的玄学调参。
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
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
    except RunRejectedError as exc:
        # WHY 必须带 Retry-After：429 只说「太多了」，客户端仍不知道何时可重试，
        # 于是只能盲猜间隔——那等于把一次明确的服务端决策变成客户端的玄学调参。
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
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


@router.post("/threads/{thread_id}/regenerate")
async def regenerate_reply(
    thread_id: str,
    body: RegenerateRequest,
    runs: RunService = Depends(get_runs),
    principal: Principal = Depends(require_permission("thread:create")),
) -> StreamingResponse:
    """重新生成最后一轮助手回复，以 SSE 流式返回。

    WHY 与编辑分成两个端点：两者的请求体本就不同（编辑必须给出下标与新文本），
    合成一个就会引入「哪些字段在哪种模式下必填」的隐含约定，而那种约定只能靠文档维持。
    """
    normalized = _validate_thread_id(thread_id)

    try:
        events = await runs.regenerate(normalized, principal=principal, model_name=body.model)
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
        # WHY 必须回 409 而不是让流里报错：分叉与运行共用同一份槽位登记，
        # 运行中发起分叉会让两轮各自读写同一会话的检查点。这个判断发生在流开始
        # 之前，所以调用方能得到一个正常的状态码。
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


@router.post("/threads/{thread_id}/edit")
async def edit_message(
    thread_id: str,
    body: EditRequest,
    runs: RunService = Depends(get_runs),
    principal: Principal = Depends(require_permission("thread:create")),
) -> StreamingResponse:
    """改写指定轮次的用户消息并从该点分叉，以 SSE 流式返回。"""
    normalized = _validate_thread_id(thread_id)

    try:
        events = await runs.edit(
            normalized,
            body.message_index,
            body.content,
            principal=principal,
            model_name=body.model,
        )
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
    except RunRejectedError as exc:
        # WHY 必须带 Retry-After：429 只说「太多了」，客户端仍不知道何时可重试，
        # 于是只能盲猜间隔——那等于把一次明确的服务端决策变成客户端的玄学调参。
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
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


@router.get("/threads/{thread_id}/branches", response_model=BranchListResult)
async def list_branches(
    thread_id: str,
    threads: ThreadService = Depends(get_threads),
    principal: Principal = Depends(require_permission("thread:read")),
) -> BranchListResult:
    """列出会话的全部分支。"""
    normalized = _validate_thread_id(thread_id)

    try:
        return await threads.list_branches(normalized, principal)
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


@router.post("/threads/{thread_id}/branches/activate", response_model=BranchListResult)
async def activate_branch(
    thread_id: str,
    branch_id: str = Query(default="", description="要切换到的分支；空串表示根分支"),
    threads: ThreadService = Depends(get_threads),
    principal: Principal = Depends(require_permission("thread:read")),
) -> BranchListResult:
    """切换当前分支，返回切换后的分支清单。

    WHY 用查询参数而不是路径段：根分支的标识是空串，路径上无法表达空值（
    ``/branches//activate`` 会被规范化掉）。切换要能回到根分支，就必须允许空值。
    """
    normalized = _validate_thread_id(thread_id)

    try:
        return await threads.activate_branch(normalized, branch_id, principal)
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
