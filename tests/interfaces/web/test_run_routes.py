"""/api/threads/{id}/runs 的会话根解析（首条消息那条路径）。

WHY 单列这一条：报过的症状是「不选工作空间就发不出第一条消息」。触发点不是跑图本身，而是
这个端点在跑图之前**无条件**做的那次根解析——附件必须与运行落在同一个工作区，所以它先取
会话根服务，而这一步用的是 ``allow_missing=True``（那时会话还没登记）。当时的实现把
「未登记 + 没给工作空间」当成「还没有根」直接 409，提示还写着「请先发出第一条消息」——
照着提示再发一次，结果一模一样。

WHY 用真注册表而不是 conftest 里的替身：替身实现的**正是**正确语义（未登记时返回专属
目录），拿它来测只会证明「替换实能通过」。这个 bug 的来源恰恰是两边不一致，所以这里直接
钉真实现。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.session_registry import SessionRegistry
from config import SessionRoot
from interfaces.web.routes import router
from runtime.thread_store import ThreadMetaStore
from tests.application.test_session_registry import _registry
from tests.conftest import make_config


class StubRuns:
    """运行服务替身：只记录入参，交出一个空的事件流。

    WHY 返回空的异步迭代器而不是抛「未实现」：本组用例要验的正是「根解析这一关过了、
    请求真的走到运行服务」，抛错会让「走到了」与「走岔了」看起来一样。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def stream(
        self, thread_id: str, content: Any, **kwargs: Any
    ) -> AsyncIterator[Any]:
        """记录本次调用并入参。"""
        self.calls.append({"thread_id": thread_id, "content": content, **kwargs})

        async def _empty() -> AsyncIterator[Any]:
            return
            yield  # 构造异步生成器：本用例不产生任何事件

        return _empty()


def _client(config: Any, registry: SessionRegistry, runs: StubRuns) -> TestClient:
    """挂上业务路由的测试客户端（状态键与 ``deps`` 读取的一致）。"""
    app = FastAPI()
    app.state.config = config
    app.state.workspaces = registry
    app.state.runs = runs
    app.include_router(router)
    return TestClient(app)


def test_first_message_without_a_workspace_is_accepted(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """不选工作空间 + 会话尚未登记：首条消息必须被接受，根落在它的专属目录。

    WHY 断言目录已建：根解析与「按需建目录」是同一件事（``SessionRegistry.resolve``
    承诺交出去的根可用）。只断言 200 而不看目录，会漏掉「解析返回了一个不存在的路径」
    这类会在下游才炸的形态。
    """
    config = make_config(tmp_path)
    registry = _registry(config, thread_store)
    runs = StubRuns()
    client = _client(config, registry, runs)

    response = client.post("/api/threads/t1/runs", json={"content": "你好"})

    assert response.status_code == 200, response.text
    assert runs.calls[0]["thread_id"] == "t1"
    assert runs.calls[0]["workspace"] is None
    assert config.session_dir("t1").is_dir()


def test_first_message_with_a_chosen_workspace_still_honours_it(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """选了工作空间就按选的来：专属目录只是「没选」时的去向。

    WHY 与上一条成对：把「未登记」那支改成回落到专属目录时，最容易顺手把请求里的取值
    忽略掉——那会让用户在草稿态选的目录**看起来**生效（界面写着它），实际文件却落在别处。
    """
    workspace = tmp_path / "project"
    workspace.mkdir()
    config = make_config(tmp_path)
    registry = _registry(config, thread_store)
    runs = StubRuns()
    client = _client(config, registry, runs)

    response = client.post(
        "/api/threads/t1/runs", json={"content": "你好", "workspace": str(workspace)}
    )

    assert response.status_code == 200, response.text
    assert runs.calls[0]["workspace"] == str(workspace)


def test_a_thread_id_with_a_separator_is_rejected_before_anything_is_created(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """带路径分隔符的会话 ID 必须被拒，且不得在工作区之外建出目录。

    WHY 走完整端点：这个 ID 是**请求里的取值**，而解析会按它派生专属目录并 mkdir。
    ``Path(sessions_root) / "..\\..\\evil"`` 在 Windows 上就是两级上行——Agent 的文件根、
    附件与产物会全部落在会话目录之外，且没有任何提示。
    """
    config = make_config(tmp_path)
    registry = _registry(config, thread_store)
    client = _client(config, registry, StubRuns())
    outside = tmp_path / "evil"

    response = client.post("/api/threads/..\\..\\evil/runs", json={"content": "你好"})

    assert response.status_code == 400, response.text
    assert not outside.exists(), "越界 ID 不得在工作区之外建出目录"


def test_first_message_with_a_scenario_assembles_the_view_for_it(
    tmp_path: Path, thread_store: ThreadMetaStore
) -> None:
    """首条消息带场景：附件那一步的根解析也必须带场景，技能视图要按它装配。

    WHY 用真注册表：这个 bug 的成因正是「解析这一步漏了场景」，而替身按服务端口径实现了场景
    传递——拿替身测只会证明替身能通过。WHY 断言磁盘上的视图：图里的技能来源就是挂载出来的
    ``/.skills-active``，视图没对齐时预设技能一个都进不了上下文，而请求本身照样 200、日志上也
    看不出异常（视图重建那条 INFO 只在装配时打印一次）。
    """
    presets = tmp_path / "presets"
    package = presets / "coding" / "commit-message"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: commit-message\ndescription: 提交信息规范\n---\n\n# 提交信息\n",
        encoding="utf-8",
    )
    (presets / "coding" / "preset.toml").write_text(
        'title = "代码开发"\nskills = ["commit-message"]\n', encoding="utf-8"
    )
    workspace = tmp_path / "project"
    workspace.mkdir()
    config = make_config(tmp_path, presets_dir=presets)
    registry = _registry(config, thread_store)
    runs = StubRuns()
    client = _client(config, registry, runs)

    response = client.post(
        "/api/threads/t1/runs",
        json={"content": "你好", "workspace": str(workspace), "preset": "coding"},
    )

    assert response.status_code == 200, response.text
    assert runs.calls[0]["preset"] == "coding"
    view = SessionRoot(config, workspace.resolve(), "coding").skill_view_store
    assert "commit-message" in {child.name for child in view.iterdir()}
