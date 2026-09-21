"""会话附件端点：上传、列举、删除。

约定：

- **会话存在性判定在服务层**：路由只负责把失败映射成状态码，「这个会话存不存在」
  只有一处判定（``AttachmentService._require_thread``）。
- **大小上限在读请求体时就要生效**：``UploadFile`` 会把大文件落到临时文件，等
  服务层再判超限时，磁盘与带宽已经付出去了。这里的截断读取是内存与磁盘的第一道闸，
  服务层的判定是第二道（它同时覆盖非 HTTP 调用方）。
"""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)

from application.attachment_service import AttachmentService, attachment_limits
from application.dto import AttachmentInfo, AttachmentLimits, AttachmentListResult
from application.errors import NotFoundError
from interfaces.web.deps import resolve_scoped_services
from interfaces.web.schemas import AttachmentDeleteResponse

logger = logging.getLogger(__name__)

# WHY 不带 prefix：同一模块里既有会话内的资源路径（``/api/threads/{id}/attachments``），
# 也有一项与会话无关的上限查询（``/api/attachments/limits``）。用 prefix 就得把前者
# 整体挪到 ``/api/threads`` 之下、再为后者单开一个 router——同一件事被拆成两处注册。
router = APIRouter(tags=["attachments"])

_CHUNK_BYTES = 64 * 1024
"""读取上传体时的分块大小。

WHY 不用 ``await file.read()`` 一次读完：那样一个远超上限的文件会先被完整读进内存，
再被判定为「太大」——拒绝的成本等于接受的成本，超限校验也就失去意义。
"""


async def get_attachments(
    request: Request,
    thread_id: str | None = None,
    workspace: str | None = Query(
        default=None,
        description=(
            "仅在该会话尚未绑定工作区时生效（新建会话上传附件就是这种情况）；"
            "已绑定的会话以绑定值为准，给出不同取值会被拒绝（409）"
        ),
    ),
) -> AttachmentService:
    """取出**该会话工作区**的附件服务。

    WHY 按会话解析：附件落在 ``<工作区>/.attachments/<thread_id>/`` 下。工作区在会话级
    可选之后不再是一个进程级常量，用全局那份就会「上传成功、却读不到」。

    WHY 单独给一个 ``workspace`` 查询参数：附件是在**首条消息之前**上传的，那一刻这条
    会话还没绑定工作区，服务端无从知道它将被指到哪。不给这个参数，新会话的附件只能落进
    启动默认工作区——而它运行在别的工作区里，附件当场失效。
    """
    bundle = await resolve_scoped_services(
        request, thread_id=thread_id, requested=workspace
    )
    return bundle.attachments


async def _read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    """按上限分块读取上传内容。

    Args:
        upload: FastAPI 传入的上传对象。
        max_bytes: 允许的最大字节数。

    Returns:
        上传内容。

    Raises:
        ValueError: 内容为空或超过上限。
    """
    buffer = bytearray()
    while True:
        chunk = await upload.read(_CHUNK_BYTES)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise ValueError(
                f"附件过大（已超过上限 {max_bytes} 字节），已中止读取；请压缩后重试"
            )
    if not buffer:
        raise ValueError("附件内容为空")
    return bytes(buffer)


@router.get("/api/attachments/limits", response_model=AttachmentLimits)
async def get_attachment_limits(request: Request) -> AttachmentLimits:
    """返回当前生效的附件上限。

    WHY 与具体会话、甚至与任何文件根都解耦：上限来自进程配置，前端要在用户**选择文件的
    那一刻**就提示「太大 / 类型不支持」。若只能按会话取，这条提示就永远晚一步——用户要
    等到上传失败才知道；而若为此去解析一个会话根，一个纯配置查询会在「会话还没有根」
    时变成 409（草稿态第一次打开就撞上）。
    """
    return attachment_limits(request.app.state.config)


@router.post("/api/threads/{thread_id}/attachments", response_model=AttachmentInfo)
async def upload_attachment(
    thread_id: str,
    file: UploadFile = File(description="要上传的图片文件"),
    service: AttachmentService = Depends(get_attachments),
) -> AttachmentInfo:
    """上传一个附件。

    成功会落一条 ``attachment_upload`` 审计（文件名 / 大小 / MIME / sha256，
    **不含文件内容**）。超限、类型不在白名单、附件数达上限都会以 400 返回并说明原因。
    """
    max_bytes = service.limits().max_bytes
    try:
        data = await _read_upload(file, max_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        return await service.upload(
            thread_id,
            filename=file.filename or "",
            mime_type=file.content_type or "",
            data=data,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("附件上传失败：thread=%s", thread_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.get("/api/threads/{thread_id}/attachments", response_model=AttachmentListResult)
async def list_attachments(
    thread_id: str,
    service: AttachmentService = Depends(get_attachments),
) -> AttachmentListResult:
    """列出某会话的附件清单及其上限。

    WHY 上限随清单一起返回：前端要在用户**选择文件的那一刻**就提示「太大 / 类型不支持」，
    而不是等上传失败；让它多打一次接口只会多一条配置漂移的路径。
    """
    try:
        return await service.list(thread_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("列举附件失败：thread=%s", thread_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.delete(
    "/api/threads/{thread_id}/attachments/{attachment_id}",
    response_model=AttachmentDeleteResponse,
)
async def delete_attachment(
    thread_id: str,
    attachment_id: str,
    service: AttachmentService = Depends(get_attachments),
) -> AttachmentDeleteResponse:
    """删除一个附件。

    WHY 需要这个入口：单会话附件数有上限，而上传错了却无法撤销时，用户会被一次
    误操作顶到上限、再也传不进去。删除是幂等的：不存在时返回 200 且 ``deleted=false``。
    """
    try:
        removed = await service.delete(thread_id, attachment_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("删除附件失败：thread=%s id=%s", thread_id, attachment_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return AttachmentDeleteResponse(
        thread_id=thread_id, attachment_id=attachment_id, deleted=removed
    )


__all__ = ["router"]
