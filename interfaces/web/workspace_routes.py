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

from application.dto import WorkspaceFileContent, WorkspaceListing
from application.errors import NotFoundError
from application.principal import Principal
from application.workspace_service import WorkspaceService
from interfaces.web.auth import require_permission
from interfaces.web.deps import require_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


def get_workspace(request: Request) -> WorkspaceService:
    """取出工作区文件服务单例。"""
    return require_state(request, "workspace", "工作区文件服务")


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
    service: WorkspaceService = Depends(get_workspace),
    principal: Principal = Depends(require_permission("file:read")),
) -> WorkspaceFileContent:
    """读取一个文件，返回文本内容、图片 data URL，或降级标记。

    读取成功会落一条 ``file_read`` 审计（含路径与大小，**不含正文**）。
    """
    try:
        return await service.read_file(path, principal)
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
