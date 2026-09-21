"""工作区文件面板端点。

约定：

- **只读**。文件面板不提供写入 / 删除——它要回答的是「Agent 产出了什么」。
  写入能力留在 Agent 自己的文件工具里（那条路径受权限与审批链约束），
  在这里再开一个入口等于多出一条绕过该链的写路径。
- **权限复用 ``file:read``**（``member`` 已持有）。不为面板单造一项权限：面板要
  展示的正是 Agent 能读的东西，两套语义漂开之后就会出现「Agent 读得到、面板看
  不到」这种自相矛盾的状态。
- **路径一律用虚拟路径**（``/react-vite-app/src/main.jsx``）。越界判定在服务层，
  路由只负责把失败映射成状态码。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from application.dto import WorkspaceFileContent, WorkspaceInfo, WorkspaceListing
from application.errors import NotFoundError
from application.principal import Principal
from application.workspace_service import WorkspaceService
from interfaces.web.auth import require_permission
from interfaces.web.deps import describe_session_root, resolve_scoped_services

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


async def get_workspace(
    request: Request,
    thread_id: str | None = Query(default=None, description="会话 ID；给了就按该会话的工作区解析"),
    workspace: str | None = Query(
        default=None,
        description="仅在该会话尚未绑定时生效（草稿态预览就是这种情况）",
    ),
) -> WorkspaceService:
    """取出**该会话工作区**的文件面板服务。

    WHY 按会话解析：工作区在会话级可选之后不再是一个进程级常量，而文件面板要展示的
    必须正是这个会话里 Agent 读写的那片目录。用全局那一个会让面板显示另一个项目的文件
    ——两个方向都不会报错，只是用户看到的不是自己刚让 Agent 改的东西。

    WHY 还要一个 ``workspace`` 参数：草稿态（尚未发出首条消息）这条会话在库里还不存在，
    而用户在界面上可能已经选了别的工作区。不认这个取值，面板就会显示启动默认目录的树
    ——用户一边看着「我选的是 B」，一边在面板里看到 A 的文件，而两边都不报错。

    WHY ``allow_missing``：同上，新会话在首条消息之前还没登记。
    """
    bundle = await resolve_scoped_services(request, thread_id=thread_id, requested=workspace)
    return bundle.files


@router.get("/info", response_model=WorkspaceInfo)
async def get_workspace_info(
    request: Request,
    thread_id: str | None = Query(default=None, description="会话 ID；给了就按该会话的根解析"),
    workspace: str | None = Query(
        default=None, description="仅在该会话尚未锁定时生效（草稿态预览就是这种情况）"
    ),
    principal: Principal = Depends(require_permission("file:read")),
) -> WorkspaceInfo:
    """返回当前会话文件根的信息。

    WHY 要有这个端点：根是**每条会话都可能不同**的运行时状态（用户选的项目，或应用给
    这条会话建的专属目录）。界面上不显示它，用户在文件面板里看到的就只是「一堆文件名」，
    无法确认那是哪个目录——而「我到底在改哪里」正是这个面板存在的意义。

    比路径多出来的两个字段各有用途：``bound`` 让界面说清这是「工作空间」还是「本会话
    专属目录」，``locked`` 让界面在首条消息之后不再提供更换入口。

    权限与文件面板同口径（``file:read``）：它暴露的是文件系统的绝对路径，
    属于文件可见性的一部分，不该另开一项权限让两套语义漂开。
    """
    return await describe_session_root(request, thread_id=thread_id, requested=workspace)


@router.get("/files", response_model=WorkspaceListing)
async def list_workspace_files(
    path: str = Query(default="/", description="目录的虚拟路径，/ 表示工作区根"),
    service: WorkspaceService = Depends(get_workspace),
    principal: Principal = Depends(require_permission("file:read")),
) -> WorkspaceListing:
    """列出工作区内一级目录（懒加载：前端按需展开下一层）。

    WHY 不落审计：列目录是高频动作且不暴露任何内容，落库只会把审计表刷成噪声，
    让真正需要留痕的读取记录淹没在里面。
    """
    try:
        return await service.list_dir(path, principal)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        # 路径非法与越界都归到 400：对调用方而言都是「这个 path 不被接受」，
        # 再细分只会额外告诉探测者「哪个路径存在」。
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("列工作区目录失败：path=%s", path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.get("/file", response_model=WorkspaceFileContent)
async def read_workspace_file(
    path: str = Query(description="文件的虚拟路径"),
    offset: int = Query(default=0, ge=0, description="起始字符偏移，用于续取大文件/长工具输出"),
    service: WorkspaceService = Depends(get_workspace),
    principal: Principal = Depends(require_permission("file:read")),
) -> WorkspaceFileContent:
    """读取一个文件，返回一段文本、图片 data URL，或降级标记。

    读取成功会落一条 ``file_read`` 审计（含路径与大小，**不含正文**）。
    响应里的 ``truncated`` 表示「后面还有」，此时用 ``offset + len(text)`` 续取
    即可拼出完整内容——「查看完整输出」用的就是这条路径。
    """
    try:
        return await service.read_file(path, principal, offset=offset)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("读取工作区文件失败：path=%s", path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


__all__ = ["router"]
