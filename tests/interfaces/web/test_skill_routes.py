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
from config import AppConfig, SessionRoot
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
    """技能视图里当前有哪些技能（视图在工作区的 ``.harness/`` 下，见 ``SessionRoot.skill_view_store``）。"""
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


# --------------------------------------------------------------- 场景预设


@pytest.fixture
async def preset_api(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, AppConfig]]:
    """带**隔离场景目录**与**已绑定场景的服务**的应用。

    WHY 这里的服务直接按 ``preset="coding"`` 构造、而不是靠查询参数传进来：本文件的替身
    （``StubSessionRegistry``）把服务整份注入，不会按请求里的根重建它。而"查询参数 → 解析出
    带场景的根 → 冲突即 409"这条链路由 ``tests/application/test_preset_binding.py`` 在注册表
    层覆盖——两条用例各管一段，合起来才是完整结论。
    """
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    presets = tmp_path / "presets"
    good = presets / "coding"
    good.mkdir(parents=True)
    (good / "preset.toml").write_text(
        'title = "代码开发"\ndescription = "面向编程任务"\nskills = ["code-review"]\n',
        encoding="utf-8",
    )
    # 一个写坏的场景：它必须出现在 problems 里，而不是从下拉里静默消失。
    broken = presets / "broken"
    broken.mkdir()
    (broken / "preset.toml").write_text("skills = [\n", encoding="utf-8")

    config = make_config(
        tmp_path, workspace=workspace, skill_dirs=[workspace / "skills"], presets_dir=presets
    )
    scope = SessionRoot(config, make_root(config).root, "coding")
    async with open_skill_store(tmp_path / "skills.db") as store:
        app = FastAPI()
        app.state.config = config
        app.state.skills = SkillService(config, scope=scope, store=store)
        app.state.workspaces = StubSessionRegistry(config, skills=app.state.skills)
        app.include_router(router)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, config


async def test_presets_endpoint_lists_scenarios_and_problems(
    preset_api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """场景清单：可用的进 ``items``，写坏的进 ``problems``。"""
    http, _ = preset_api

    response = await http.get("/api/presets")

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == ["coding"]
    assert body["items"][0]["title"] == "代码开发"
    assert body["items"][0]["skills"] == ["code-review"]
    assert [Path(item["directory"]).name for item in body["problems"]] == ["broken"]


async def test_skills_endpoint_reports_the_scenario(
    preset_api: tuple[httpx.AsyncClient, AppConfig],
) -> None:
    """清单能看出「这条会话属于哪个场景」，以及每个技能是否在白名单内。"""
    http, config = preset_api
    _write_skill(config, "code-review")
    _write_skill(config, "outside")

    body = (await http.get("/api/skills", params=_SESSION)).json()

    assert body["preset"]["id"] == "coding"
    assert body["preset_id"] == "coding"
    by_name = {item["name"]: item for item in body["items"]}
    assert by_name["code-review"]["in_preset"] is True
    assert by_name["outside"]["in_preset"] is False
    # 分类由来源推导：显式配的目录名就是 ``skills``，其虚拟路径即 ``/skills``（用户技能库），
    # 因此如实标成 user——而不是硬套「显式配置 = custom」。判据是挂在哪，不是谁指定的。
    assert by_name["code-review"]["category"] == "user"
