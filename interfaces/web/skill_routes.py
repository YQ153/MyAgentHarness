"""技能库端点：查看技能清单与启停状态、启用 / 停用。

约定：

- **技能集是全应用共享的一份**（物化视图在工作区里只有一个，作用域固定 global），
  启停影响所有会话——因此响应里会带上重建结果，让「我停用了但没生效」这类疑问
  有据可查。
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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from application.errors import NotFoundError
from application.skill_service import SkillService, list_presets
from interfaces.web.deps import require_state, resolve_scoped_services
from interfaces.web.schemas import (
    PresetListResponse,
    SkillListResponse,
    SkillToggleRequest,
    SkillToggleResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])


async def get_skills(
    request: Request,
    thread_id: str | None = Query(default=None, description="会话 ID；给了就按该会话的工作区解析"),
    workspace: str | None = Query(
        default=None,
        description="仅在该会话尚未绑定时生效（草稿态预览就是这种情况）",
    ),
    preset: str | None = Query(
        default=None,
        description="场景预设 ID；草稿态下按它预览技能集，已锁定会话以库里的场景为准",
    ),
) -> SkillService:
    """取出**该会话工作区**的技能库服务。

    WHY 按会话解析：物化视图（``/.skills-active``）、用户技能目录与**场景预设**都是按工作区
    各一份的衍生物。用全局那一个会让面板显示 A 工作区的技能集，而 Agent 在 B 工作区里按
    另一份做事——两边都不报错，用户看到的却是两套事实。

    WHY 还要一个 ``workspace`` 参数：草稿态下这条会话还没登记，而用户可能已经选了别的
    工作区；不认这个取值，面板展示的技能集与即将使用的那份就是两回事。

    WHY 还要一个 ``preset`` 参数：同理——用户在界面上选了场景、还没发第一条消息时，面板
    就应当按**该场景**展示技能集；否则"选了场景"这件事在面板上完全看不出来。

    WHY ``allow_missing``：同上，新会话在首条消息之前还没登记。
    """
    bundle = await resolve_scoped_services(
        request, thread_id=thread_id, requested=workspace, preset=preset
    )
    return bundle.skills


@router.get("/api/presets", response_model=PresetListResponse)
async def list_preset_catalog(request: Request) -> PresetListResponse:
    """列出可用的**场景预设**（供新建会话时选择）。

    WHY 不需要会话上下文：场景清单是产品能力公示（磁盘上有哪些 ``preset.toml``），与会话、
    工作区都无关。要求"先有会话才能看场景"会让「新建会话时选场景」变成循环依赖。

    WHY 与 ``GET /api/skills`` 分开：技能清单回答「这个工作空间里现在有什么」（按会话解析），
    场景清单回答「产品交付了哪些场景」（全局一份）。混在一个响应里，两者的作用域会含糊。

    ``problems`` 里是写坏的 ``preset.toml``——不报出来的话，那个场景只会从下拉里静默消失，
    而配置作者完全没有线索。
    """
    config = require_state(request, "config", "应用配置")
    catalog = list_presets(config)
    return PresetListResponse.model_validate(
        {
            "items": [
                {
                    "id": preset.preset_id,
                    "title": preset.title,
                    "description": preset.description,
                    "skills": list(preset.skills),
                }
                for preset in catalog.presets
            ],
            "problems": [
                {"directory": item.directory, "reason": item.reason} for item in catalog.problems
            ],
        }
    )


@router.get("/api/skills", response_model=SkillListResponse)
async def list_skills(
    service: SkillService = Depends(get_skills),
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
    request: Request,
    service: SkillService = Depends(get_skills),
) -> SkillToggleResponse:
    """启用或停用一个技能，并立即重建物化视图。

    为何必须重建视图：建图的技能来源指向那份派生产物，只改数据库不重建视图的表现是
    「我停用了它，Agent 还在用」——且两者都不会报错。

    WHY 顺带重建**所有已装配根**的视图：``set_enabled`` 只能重建它自己那个根的视图（它手上
    只有本会话的 scope），而技能启停是**全局**的、视图却按工作区各一份。漏掉这一步的表现是
    「在 A 工作区停用了某技能，切到 B 工作区它还在」——尤其是多场景并存时，B 工作区正是另一
    个场景的会话。

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

    # WHY 这里的失败只记日志、不改变响应：启停本身已经落库（那是唯一真相），其余根的视图
    # 会在它们下一次装配或下一次启停时收敛。把一次成功的启停变成 500，只会让用户重试——
    # 而重试并不能更快地修好别处的视图。
    registry = getattr(request.app.state, "workspaces", None)
    if registry is not None:
        try:
            await registry.refresh_views()
        except Exception:  # noqa: BLE001 - 旁路动作，不能反过来否决主流程
            logger.exception("重建其余工作空间的技能视图失败：skill=%s", name)

    return SkillToggleResponse.model_validate(result)
