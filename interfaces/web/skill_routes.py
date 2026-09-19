"""技能库端点：查看技能清单与启停状态、启用 / 停用。

约定：

- **技能集是全应用共享的一份**（物化视图在工作区里只有一个，作用域固定 global），
  因此停用是管理员操作：查看清单用 ``skill:read``（member 已持有，与 ``tool:read``
  同一理由——「这个助手会加载哪些技能」是使用者的基本知情项），启停用 ``skill:write``
  且**只有 admin 持有**。理由见 ``application.principal`` 里的权限注释。
- **启停只对之后新建的会话生效**：技能索引由上游在每个会话的第一次运行时加载一次并
  写进该会话的状态。这不是本模块能改变的（属上游设计），但必须在接口上写清楚，
  否则用户会以为「停用了但没生效」是缺陷。响应里带 ``rebuild`` 说明，文档字符串也写明。
- **不做技能内容编辑**：技能包的增删改都由使用者直接改文件系统（它们是普通目录）。
  在这里开一个「上传技能包」的入口，等于让 HTTP 层成为第二个能往工作区写指令文本的
  路径——而技能正文是会被注入模型上下文的**指令**，那条路径应当只有一条（文件系统），
  并同样受审批与审计约束。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from application.errors import NotFoundError
from application.principal import Principal
from application.skill_service import SkillService
from interfaces.web.auth import require_permission
from interfaces.web.deps import require_state
from interfaces.web.schemas import (
    SkillListResponse,
    SkillToggleRequest,
    SkillToggleResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])


def get_skills(request: Request) -> SkillService:
    """取出技能库服务单例。"""
    return require_state(request, "skills", "技能库服务")


@router.get("/api/skills", response_model=SkillListResponse)
async def list_skills(
    service: SkillService = Depends(get_skills),
    principal: Principal = Depends(require_permission("skill:read")),
) -> SkillListResponse:
    """返回技能清单、启用状态与加载诊断。

    响应里的 ``unloadable`` / ``load_errors`` 是刻意暴露的：上游对单条技能的解析失败
    只写日志、不放进它的返回值，不在这里报出来，用户看到的就是「我明明建了它，面板里
    却没有」，且没有任何可查的线索。

    ``view_warning`` 非空表示物化视图不可用——此时建图会退回「全部技能」并在日志告警，
    也即**启停当前不生效**。把它随清单下发，是为了让界面能说清「你点的停用为什么没用」。
    """
    return SkillListResponse.model_validate(await service.list_skills())


@router.patch("/api/skills/{name}", response_model=SkillToggleResponse)
async def toggle_skill(
    name: str,
    payload: SkillToggleRequest,
    service: SkillService = Depends(get_skills),
    principal: Principal = Depends(require_permission("skill:write")),
) -> SkillToggleResponse:
    """启用或停用一个技能，并立即重建物化视图。

    为何必须重建视图：建图的技能来源指向那份派生产物，只改数据库不重建视图的表现是
    「我停用了它，Agent 还在用」——且两者都不会报错。

    启停**对已在进行的会话无效**：技能索引在每个会话第一次运行时加载一次并写进会话状态，
    那些会话会沿用已加载的那份；新建会话才按新状态加载。

    Raises:
        HTTPException: 404 技能名不在已加载的清单里；400 参数非法。
    """
    try:
        result = await service.set_enabled(name, payload.enabled)
    except NotFoundError as exc:
        # 技能名多来自界面或脚本，写错一个字母若被容忍，会留下一条看起来完全正常、
        # 却永不生效的记录——用户以为自己停用了它。
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return SkillToggleResponse.model_validate(result)
