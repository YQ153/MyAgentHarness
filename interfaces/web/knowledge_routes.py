"""知识库端点：查看索引清单、索引工作区文档、移除单份文档。

约定：

- **索引与检索都在服务层**（``KnowledgeService``）：路由只把失败映射成状态码，
  不在这一层再实现一次切分、嵌入或去重口径。
- **切分与嵌入都不在这里**：本模块只把参数交给 ``KnowledgeService``。索引是一项耗时
  操作（要调用嵌入），把它写在路由里会让「HTTP 层管了业务」这件事从第一天就成立。

WHY 没有检索端点：检索已经以工具形式交付（``search_documents``，见 ``knowledge_tools``），
Agent 与使用者走的是同一条实现。再加一个 REST 检索端点会形成第二条路径，两条路径的
排序与融合策略迟早分叉；面板若需要预览，应复用服务层的 ``search``。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from application.knowledge_service import KnowledgeService
from interfaces.web.deps import resolve_scoped_services
from interfaces.web.schemas import (
    KnowledgeDeleteResponse,
    KnowledgeIndexItem,
    KnowledgeIndexRequest,
    KnowledgeIndexResponse,
    KnowledgeListResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge"])

_STATUS_KEYS = ("indexed", "unchanged", "empty", "skipped")
"""逐份文档的可能状态；与 ``KnowledgeService.index_workspace`` 的汇总口径一致。"""


async def get_knowledge(
    request: Request,
    thread_id: str | None = Query(default=None, description="会话 ID；给了就按该会话的工作区解析"),
    workspace: str | None = Query(
        default=None,
        description="仅在该会话尚未绑定时生效（草稿态预览就是这种情况）",
    ),
) -> KnowledgeService:
    """取出**该会话工作区**的知识库服务。

    WHY 按会话解析：知识库按工作区各存一份索引（库里以「工作区内的虚拟路径」为键去重，
    两个项目的 ``/README.md`` 是同一个键）。用全局那一个会让界面列出别的项目的文档，
    而 Agent 在本次会话里检索到的却是另一份——两个方向都不会报错。

    WHY 还要一个 ``workspace`` 参数：草稿态下这条会话还没登记，而用户可能已经选了别的
    工作区；不认这个取值，面板列的就是另一个项目的文档。

    WHY 与工具取的是同一份：工具侧同样按「本轮运行的工作区」解析（见
    ``knowledge_tools``），两边都落在 ``knowledge_runtime`` 的同一张句柄表上。
    """
    bundle = await resolve_scoped_services(request, thread_id=thread_id, requested=workspace)
    return bundle.knowledge


def _as_response(summary: dict[str, Any]) -> KnowledgeIndexResponse:
    """把服务层的汇总整理成响应模型。

    WHY 需要这一层转换：服务层返回的是普通字典（它不认识 HTTP 模型），而单文档索引
    与工作区索引的返回形状不同——把两者的归一放在路由里，服务层就不必为了 HTTP 的
    形状而改变自己的返回。
    """
    items = [KnowledgeIndexItem.model_validate(item) for item in summary.get("items", [])]
    return KnowledgeIndexResponse(
        scanned=int(summary.get("scanned", len(items))),
        indexed=int(summary.get("indexed", 0)),
        unchanged=int(summary.get("unchanged", 0)),
        empty=int(summary.get("empty", 0)),
        skipped=int(summary.get("skipped", 0)),
        items=items,
    )


def _single_summary(result: dict[str, Any]) -> dict[str, Any]:
    """把单文档索引结果整理成与工作区索引同形的汇总。"""
    state = str(result.get("status", ""))
    summary: dict[str, Any] = {"scanned": 1, "items": [result]}
    for key in _STATUS_KEYS:
        summary[key] = 1 if state == key else 0
    return summary


@router.get("/api/knowledge", response_model=KnowledgeListResponse)
async def list_knowledge(
    service: KnowledgeService = Depends(get_knowledge),
) -> KnowledgeListResponse:
    """返回已索引的文档、索引规模与知识库能力。"""
    return KnowledgeListResponse.model_validate(await service.list_documents())


@router.post("/api/knowledge", response_model=KnowledgeIndexResponse)
async def index_knowledge(
    payload: KnowledgeIndexRequest | None = Body(default=None),
    service: KnowledgeService = Depends(get_knowledge),
) -> KnowledgeIndexResponse:
    """索引工作区中的文本文档；给出 ``path`` 时只索引那一份。

    无法索引的文件（二进制、非 UTF-8、超过字节上限）按条跳过并计入 ``skipped``，
    逐条原因在 ``items[].detail`` 里——一份怪文件不该让整次索引失败。

    Raises:
        HTTPException: 400 路径非法 / 文件不适合索引；404 指定文件不存在。
    """
    request_body = payload or KnowledgeIndexRequest()
    try:
        if request_body.path:
            summary = _single_summary(
                await service.index_document(
                    request_body.path, force=request_body.force
                )
            )
        else:
            summary = await service.index_workspace(force=request_body.force)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        # ``UnsupportedDocumentError`` 与 ``WorkspacePathError`` 都是它的子类：
        # 前者是「这个文件不适合做文档」，后者是「这个路径不合法」，处置相同。
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return _as_response(summary)


@router.delete("/api/knowledge", response_model=KnowledgeDeleteResponse)
async def delete_knowledge(
    source_path: str = Query(alias="path", description="要移除的源文件虚拟路径"),
    service: KnowledgeService = Depends(get_knowledge),
) -> KnowledgeDeleteResponse:
    """从知识库移除一份文档；**不影响工作区里的源文件**。

    WHY 用查询参数而不是路径参数：虚拟路径本身含 ``/``，放进路径段就得靠百分号编码
    才能传对，而编码错的形式在日志里几乎无法辨认。

    Raises:
        HTTPException: 400 路径非法。
    """
    try:
        deleted = await service.remove_document(source_path)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return KnowledgeDeleteResponse(source_path=source_path, deleted=deleted)
