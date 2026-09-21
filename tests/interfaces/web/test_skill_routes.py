"""技能端点的回归测试。

覆盖面：清单与诊断字段、启停真的改变了视图、未知技能名 404、以及请求体校验。

WHY 用 httpx 的 ASGITransport 而不是 ``TestClient``：与知识库、附件端点同一理由——
这些用例要读真实的 ``SkillStateStore``（异步连接），而 ``TestClient`` 自带事件循环。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from application.skill_service import SkillService
from config import AppConfig
from interfaces.web.skill_routes import router
from runtime.skill_store import open_skill_store
from tests.conftest import StubSessionRegistry, make_config, make_root

_SKILL = """---
name: {name}
description: {name} 的说明
---

# {name}
"""

#: 面板端点按**会话**解析文件根；用例统一带上一个会话 ID（与界面真实请求一致）。
_SESSION = {"thread_id": "t1"}


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, AppConfig]]:
    """只挂技能路由的应用（与真实启动共用同一个服务实现）。"""
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    config = make_config(tmp_path, workspace=workspace, skill_dirs=[workspace / "skills"])
    async with open_skill_store(tmp_path / "skills.db") as store:
        app = FastAPI()
        app.state.config = config
        app.state.skills = SkillService(config, scope=make_root(config), store=store)
        app.state.workspaces = StubSessionRegistry(config, skills=app.state.skills)
        app.include_router(router)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, config


def _write_skill(config: AppConfig, name: str, *, body: str | None = None) -> None:
    """在用户技能目录里写一个技能包。"""
    directory = Path(make_root(config).root) / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        body if body is not None else _SKILL.format(name=name), encoding="utf-8"
    )


def _view_names(config: AppConfig) -> set[str]:
    """技能视图里当前有哪些技能（视图在**根外存储**里，见 ``SessionRoot.skill_view_store``）。"""
    view = make_root(config).skill_view_store
    return {child.name for child in view.iterdir() if child.is_dir()} if view.is_dir() else set()


# --------------------------------------------------------------- 清单


async def test_list_returns_skill(api: tuple[httpx.AsyncClient, AppConfig]) -> None:
    """清单返回技能名、说明与启用状态。"""
    http, config = api
    _write_skill(config, "code-review")

    response = await http.get("/api/skills", params=_SESSION)

    assert response.status_code == 200
    body = response.json()
    assert [(item["name"], item["enabled"]) for item in body["items"]] == [("code-review", True)]
    assert body["scope"] == "global"


async def test_list_exposes_unloadable_reason(
    api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """没能加载的候选目录连同原因一起下发。

    WHY：上游对单条技能的解析失败只写日志、不放进返回值。不报出来，用户看到的就是
    「我明明建了它，面板里却没有」，且没有任何可查的线索。
    """
    http, config = api
    broken = Path(make_root(config).root) / "skills" / "no-desc"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "SKILL.md").write_text('---\nname: "no-desc"\n---\n\n# 正文\n', encoding="utf-8")

    body = (await http.get("/api/skills", params=_SESSION)).json()

    assert body["items"] == []
    assert [item["directory"] for item in body["unloadable"]] == ["/skills/no-desc"]
    assert "description" in body["unloadable"][0]["reason"]


async def test_list_warns_when_view_missing(
    api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """视图不存在时给出降级说明——否则「我点停用没生效」没有任何线索。"""
    http, config = api
    _write_skill(config, "code-review")

    body = (await http.get("/api/skills", params=_SESSION)).json()

    assert body["view_exists"] is False
    assert "不存在" in body["view_warning"]
    assert body["graph_sources"] == ["/skills"]


async def test_list_reports_view_once_rebuilt(
    api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """启停过一次之后视图就位，来源切换为视图且不再告警。"""
    http, config = api
    _write_skill(config, "code-review")

    await http.patch("/api/skills/code-review", params=_SESSION, json={"enabled": True})
    body = (await http.get("/api/skills", params=_SESSION)).json()

    assert body["view_exists"] is True
    assert body["view_warning"] == ""
    assert body["graph_sources"] == ["/.skills-active"]


# --------------------------------------------------------------- 启停


async def test_disable_removes_skill_from_view(
    api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """停用后视图里不再有它。

    WHY 断言视图而不只是响应：只写库、没重建视图的表现是「我停用了它，Agent 还在用」，
    而两者都不会报错——只有视图内容能证明启停真的生效。
    """
    http, config = api
    _write_skill(config, "code-review")
    _write_skill(config, "legacy")

    response = await http.patch("/api/skills/legacy", params=_SESSION, json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["view"]["enabled_skills"] == ["code-review"]
    assert _view_names(config) == {"code-review"}


async def test_enable_puts_skill_back(api: tuple[httpx.AsyncClient, AppConfig]) -> None:
    """重新启用后它回到视图里，清单状态同步。"""
    http, config = api
    _write_skill(config, "legacy")
    await http.patch("/api/skills/legacy", params=_SESSION, json={"enabled": False})

    await http.patch("/api/skills/legacy", params=_SESSION, json={"enabled": True})

    assert _view_names(config) == {"legacy"}
    body = (await http.get("/api/skills", params=_SESSION)).json()
    assert body["items"][0]["enabled"] is True


async def test_unknown_skill_returns_404(api: tuple[httpx.AsyncClient, AppConfig]) -> None:
    """启停不存在的技能名返回 404 而不是静默成功。"""
    http, _ = api

    response = await http.patch("/api/skills/不存在的技能", params=_SESSION, json={"enabled": False})

    assert response.status_code == 404


async def test_missing_body_field_is_rejected(api: tuple[httpx.AsyncClient, AppConfig]) -> None:
    """``enabled`` 必填——缺省成某个值会让「停用」被误读成「启用」。"""
    http, config = api
    _write_skill(config, "code-review")

    response = await http.patch("/api/skills/code-review", params=_SESSION, json={})

    assert response.status_code == 422
