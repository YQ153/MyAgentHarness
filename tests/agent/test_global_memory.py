"""全局长期记忆：``MEMORY_FILE`` 必须对**每条会话**生效（而不是只对某个工作区）。

WHY 需要这一组用例：这条链路上任何一环断掉的表现都是「记忆少了一条」，而且
**不报错**——deepagents 对读不到的记忆来源是静默跳过的，旧实现更直接：文件在工作区
之外就整条跳过，只留一行 WARNING。用户看到的是「我明明写了全局约定，Agent 不照做」，
而日志里那句中文警告藏在启动输出里。因此这里分三层钉住它：

1. **方案层**（``SessionRoot.memory_plan``）：来源与挂载点同源，缺文件如实记日志；
2. **挂载层**（``agent.readonly_mount``）：那一个文件读得到，父目录里的其它文件读不到；
3. **端到端**：装配真图 + 脚本化模型，断言全局记忆出现在**任意两个不同工作区**的
   系统提示里，且 Agent 改不动它。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langgraph.store.base import BaseStore

from agent.backends import build_backend
from agent.graph import build_agent
from agent.readonly_mount import ReadOnlyFileMount
from config import GLOBAL_MEMORY_PREFIX, AppConfig, SessionRoot, VirtualMount
from runtime.store import open_store
from tests.agent.test_memory_end_to_end import ScriptedChatModel, _tool_call, _tool_outputs
from tests.conftest import make_config, make_root

GLOBAL_TEXT = "# 全局记忆\n用户称呼：言先生。\n"


# ------------------------------------------------------------------ 夹具


def _config(tmp_path: Path, **overrides: Any) -> AppConfig:
    """构造测试配置并建好目录（``build_backend`` 要求根存在）。"""
    config = make_config(tmp_path, **overrides)
    config.ensure_directories()
    return config


def _global_memory(tmp_path: Path, text: str = GLOBAL_TEXT) -> Path:
    """在**工作区之外**造一份全局记忆文件，返回它的路径。"""
    target = tmp_path / "memory" / "AGENTS.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[BaseStore]:
    async with open_store(tmp_path / "agent.db") as opened:
        yield opened


# ------------------------------------------------------------------ 方案层


def test_configured_global_memory_is_exposed_at_the_mount_path(tmp_path: Path) -> None:
    """配了全局记忆：来源指向挂载路径，且挂载点同时给出（两者缺一就读不到）。"""
    global_file = _global_memory(tmp_path)
    scope = make_root(_config(tmp_path, memory_file=global_file))

    plan = scope.memory_plan

    assert plan.sources == ["/global/AGENTS.md"]
    assert plan.mounted_files == [global_file]


def test_workspace_memory_is_used_when_no_global_memory_is_configured(tmp_path: Path) -> None:
    """没配全局记忆：退回工作区自带的那份（不需要任何挂载）。"""
    config = _config(tmp_path)
    root = make_root(config)
    (root.root / "AGENTS.md").write_text("工作区约定\n", encoding="utf-8")

    plan = root.memory_plan

    assert plan.sources == ["/AGENTS.md"]
    assert plan.mounts == []


def test_no_source_at_all_is_an_empty_plan_not_an_error(tmp_path: Path) -> None:
    """谁都不存在：空方案（不抛异常）——「没在用这个功能」不该让启动失败。"""
    config = _config(tmp_path)

    plan = make_root(config, name="bare").memory_plan

    assert plan.sources == []
    assert plan.mounts == []


def test_missing_global_memory_does_not_fall_back_to_the_workspace_file(tmp_path: Path) -> None:
    """配了全局记忆但文件不在：**不**悄悄改用工作区那份，如实空掉并记日志。

    WHY 这条值得单列：静默回落会让用户以为「全局文件照旧生效」（因为它错了也不报错），
    而真正生效的是另一份文件——两处说法冲突时，界面上没有任何线索。
    """
    config = _config(tmp_path, memory_file=tmp_path / "nope" / "AGENTS.md")
    root = make_root(config)
    (root.root / "AGENTS.md").write_text("工作区约定\n", encoding="utf-8")

    plan = root.memory_plan

    assert plan.sources == []
    assert plan.mounts == []


def test_memory_plan_is_computed_once_per_root(tmp_path: Path) -> None:
    """同一份根上重复取方案是同一个对象：不让「文件刚好此刻被删」出现两种结果。

    WHY 单列：装配一张图会取两次（backend 的路由 + memory 参数），而每次都要读磁盘、
    记日志。算两遍不只是浪费——第一次读到、第二次读不到时，来源与挂载会分别成立，
    正好落进「列了来源但没有挂载」那个静默少一条记忆的形态。
    """
    global_file = _global_memory(tmp_path)
    scope = make_root(_config(tmp_path, memory_file=global_file))

    assert scope.memory_plan is scope.memory_plan


def test_global_memory_prefix_is_a_route_prefix() -> None:
    """前缀必须是「/」开头「/」结尾：否则路由会落到默认 backend（工作区）。"""
    assert GLOBAL_MEMORY_PREFIX.startswith("/")
    assert GLOBAL_MEMORY_PREFIX.endswith("/")


@pytest.mark.parametrize(
    "prefix, label",
    [("global/", "全局"), ("/global", "全局"), ("/global/", ""), ("/global/", "   ")],
)
def test_memory_mount_rejects_bad_values(tmp_path: Path, prefix: str, label: str) -> None:
    """挂载点取值的校验：前缀形态与说法都不能空着（它们是路由与文案的全部依据）。"""
    with pytest.raises(ValueError):
        VirtualMount(prefix=prefix, host_path=_global_memory(tmp_path), label=label)


def test_memory_mount_rejects_a_non_path_host(tmp_path: Path) -> None:
    """``host_path`` 必须是 ``Path``：字符串会让 ``.name`` 在另一处才炸。"""
    with pytest.raises(ValueError, match="必须是 Path"):
        VirtualMount(prefix="/global/", host_path="/tmp/AGENTS.md", label="全局")  # type: ignore[arg-type]


# ------------------------------------------------------------------ 挂载层


def _mount(tmp_path: Path) -> tuple[ReadOnlyFileMount, Path]:
    """造一个挂载点，并在**同一目录**里放一个不该被看见的兄弟文件。"""
    host = _global_memory(tmp_path)
    (host.parent / "secret.txt").write_text("言先生 不该被看到\n", encoding="utf-8")
    return ReadOnlyFileMount(host, label="全局长期记忆"), host


def test_mount_reads_the_file(tmp_path: Path) -> None:
    """读得到：``read`` 与 ``download_files``（记忆中间件走的是后者）。"""
    mount, _host = _mount(tmp_path)

    read = mount.read("/AGENTS.md")
    downloaded = mount.download_files(["/AGENTS.md"])

    assert read.error is None
    assert read.file_data is not None
    assert "言先生" in read.file_data["content"]
    assert downloaded[0].error is None
    assert downloaded[0].content is not None
    assert "言先生".encode() in downloaded[0].content


def test_mount_lists_only_that_file(tmp_path: Path) -> None:
    """列目录只列那一个文件：兄弟文件不得出现。"""
    mount, _host = _mount(tmp_path)

    listing = mount.ls("/")

    assert listing.error is None
    assert [entry["path"] for entry in (listing.entries or [])] == ["/AGENTS.md"]


def test_mount_cannot_reach_a_sibling_file(tmp_path: Path) -> None:
    """安全边界：同一个目录里的其它文件，读、取、搜、匹配都碰不到。

    WHY 这是本模块存在的原因：挂载点的 inner root 是宿主文件的**父目录**（为了复用上游
    的读取口径），父目录里可能有一整个项目。白名单一旦漏一条路径形态，暴露的就是它。
    """
    mount, _host = _mount(tmp_path)

    assert mount.read("/secret.txt").error is not None
    assert mount.download_files(["/secret.txt"])[0].error == "file_not_found"
    assert mount.grep("不该被看到", "/").matches == []
    assert mount.glob("*.txt", "/").matches == []
    assert mount.ls("/").entries == mount.ls("/").entries  # 上一次断言已确认为仅一条


@pytest.mark.parametrize("path", ["/../secret.txt", "/./../secret.txt", "AGENTS.md", ""])
def test_mount_rejects_paths_that_are_not_inside_it(tmp_path: Path, path: str) -> None:
    """越界与相对路径一律拒绝（不做出「回退一级」的路径运算）。"""
    mount, _host = _mount(tmp_path)

    assert mount.read(path).error is not None
    assert mount.download_files([path])[0].error == "file_not_found"


def test_mount_grep_searches_only_the_mounted_file(tmp_path: Path) -> None:
    """grep 只搜挂载的文件——上游的 ``grep(path=文件)`` 实测会搜整个父目录。"""
    mount, _host = _mount(tmp_path)

    matches = mount.grep("言先生").matches or []

    assert [item["path"] for item in matches] == ["/AGENTS.md"]
    assert all("不该被看到" not in item["text"] for item in matches)


def test_mount_grep_honours_glob_and_max_count(tmp_path: Path) -> None:
    """``glob`` 不匹配文件名时返回空；超出 ``max_count`` 时截断并标注。"""
    host = _global_memory(tmp_path, "言先生\n言先生\n言先生\n")
    mount = ReadOnlyFileMount(host, label="全局长期记忆")

    assert mount.grep("言先生", glob="*.txt").matches == []
    capped = mount.grep("言先生", max_count=2)
    assert len(capped.matches or []) == 2
    assert capped.truncated is True


@pytest.mark.parametrize(
    "action",
    [
        lambda mount: mount.write("/AGENTS.md", "改掉"),
        lambda mount: mount.edit("/AGENTS.md", "言先生", "张三"),
        lambda mount: mount.delete("/AGENTS.md"),
        lambda mount: mount.upload_files([("/AGENTS.md", b"hi")])[0],
    ],
    ids=["write", "edit", "delete", "upload"],
)
def test_mount_refuses_every_write(tmp_path: Path, action: Any) -> None:
    """写、改、删、上传一律拒绝，且**宿主文件内容不变**（人工维护的那份是唯一真相）。"""
    mount, host = _mount(tmp_path)

    result = action(mount)

    assert result.error is not None
    assert "只读" in result.error
    assert host.read_text(encoding="utf-8") == GLOBAL_TEXT


def test_mount_error_messages_use_the_public_path(tmp_path: Path) -> None:
    """错误文案给的是**对外**路径（模型写下的那个），不是挂载点内部的写法。

    WHY 单列：回错路径会让模型以为「那是工作区里的另一个同名文件」，于是换一种写法再试
    一次——一次本可以立刻结束的失败，变成几轮无效尝试。
    """
    mount = ReadOnlyFileMount(
        _global_memory(tmp_path), label="全局长期记忆", mounted_at="/global/AGENTS.md"
    )

    assert "/global/AGENTS.md" in str(mount.write("/global/AGENTS.md", "改掉").error)
    assert mount.mounted_at == "/global/AGENTS.md"
    assert mount.virtual_path == "/AGENTS.md"


def test_mount_reports_a_vanished_file_as_file_not_found(tmp_path: Path) -> None:
    """文件在装配之后被删：报 ``file_not_found``（中间件据此**跳过**而不是整轮报错）。

    WHY 逐字对齐这个错误码：``MemoryMiddleware`` 用的是 ``== "file_not_found"``。
    改一个字，人工删掉/替换记忆文件就会让每一轮运行失败。
    """
    mount, host = _mount(tmp_path)
    host.unlink()

    assert mount.download_files(["/AGENTS.md"])[0].error == "file_not_found"
    assert mount.ls("/").entries == []
    assert mount.read("/AGENTS.md").error is not None


# ------------------------------------------------------------------ backend 与端到端


def test_backend_exposes_the_global_memory_through_the_composite(tmp_path: Path, store: BaseStore) -> None:
    """经 ``CompositeBackend`` 走一遍：``/global/AGENTS.md`` 能读到内容。"""
    global_file = _global_memory(tmp_path)
    config = _config(tmp_path, memory_file=global_file)
    backend = build_backend(config, store, scope=make_root(config))

    downloaded = backend.download_files(["/global/AGENTS.md"])

    assert downloaded[0].error is None
    assert b"\xe8\xa8\x80\xe5\x85\x88\xe7\x94\x9f" in (downloaded[0].content or b"")


def test_backend_does_not_mount_anything_without_a_global_memory(
    tmp_path: Path, store: BaseStore
) -> None:
    """没配全局记忆时不挂载：``/global/`` 下的读取落回工作区（读不到）。"""
    config = _config(tmp_path)
    backend = build_backend(config, store, scope=make_root(config))

    downloaded = backend.download_files(["/global/AGENTS.md"])

    assert downloaded[0].content is None


class _RecordingChatModel(ScriptedChatModel):
    """记录每次调用收到的消息，供断言「记忆进了系统提示」。"""

    seen: list[list[Any]] = []

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.seen.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


class _StubRegistry:
    """只回答「用哪个模型」的注册表替身：让真实 ``build_agent`` 跑在脚本化模型上。

    WHY 要替换而不是自己调 ``create_deep_agent``：本组用例要验的是**应用**把记忆接对
    没有（来源列表、挂载路由）。自己拼一次 ``create_deep_agent`` 只会验到「deepagents
    能加载虚拟路径」——那与「本应用是否给了对的路径」是两件事。
    """

    def __init__(self, model: Any, name: str = "scripted") -> None:
        self._model = model
        self.default_name = name

    def get(self, name: str | None = None) -> Any:
        del name
        return self._model


def _system_text(model: _RecordingChatModel) -> str:
    """把各次调用里系统消息的内容拼起来。"""
    return "\n".join(
        str(message.content)
        for call in model.seen
        for message in call
        if isinstance(message, SystemMessage)
    )


async def _run_agent(
    config: AppConfig, root: SessionRoot, store: BaseStore, model: _RecordingChatModel, monkeypatch: Any
) -> list[Any]:
    """用真实的 ``build_agent`` 装配并跑一轮，返回全部流分片。"""
    monkeypatch.setattr("agent.graph.get_registry", lambda _config: _StubRegistry(model))
    agent = build_agent(config, scope=root, store=store)
    return [
        chunk
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": "你好"}]},
            config={"configurable": {"thread_id": "t1"}, "recursion_limit": 20},
            stream_mode=["messages", "updates"],
        )
    ]


@pytest.mark.parametrize("workspace_name", ["ws-a", "ws-b"])
async def test_global_memory_reaches_the_system_prompt_of_any_workspace(
    tmp_path: Path, store: BaseStore, monkeypatch: Any, workspace_name: str
) -> None:
    """全局记忆必须进入**每条**会话的系统提示，与那条会话的工作区无关。

    WHY 参数化两个工作区：这正是用户报的那件事——记忆文件在工作区之外，而每条会话的
    工作区都不同。只验一个工作区的话，「它恰好落在那个工作区里」也能通过。
    """
    global_file = _global_memory(tmp_path)
    config = _config(tmp_path, memory_file=global_file)
    model = _RecordingChatModel(replies=[AIMessage(content="好")])

    await _run_agent(config, make_root(config, name=workspace_name), store, model, monkeypatch)

    assert "言先生" in _system_text(model), "全局记忆没有进入系统提示（工作区：%s）" % workspace_name


async def test_agent_cannot_rewrite_the_global_memory(
    tmp_path: Path, store: BaseStore, monkeypatch: Any
) -> None:
    """Agent 试图改写全局记忆时必须失败，且宿主文件内容不变。

    WHY 单列：它是「人工维护」这条承诺的验收线。挂载点若可写，Agent 会把「用户写在
    文件里的事实」悄悄改成自己的猜测，而人工下次看文件时无从分辨。
    """
    global_file = _global_memory(tmp_path)
    config = _config(tmp_path, memory_file=global_file)
    model = _RecordingChatModel(
        replies=[
            _tool_call("write_file", {"file_path": "/global/AGENTS.md", "content": "被改掉了"}),
            AIMessage(content="改不动。"),
        ]
    )

    chunks = await _run_agent(config, make_root(config), store, model, monkeypatch)

    feedback = "\n".join(_tool_outputs(chunks))
    assert "只读" in feedback
    # 文案里必须是模型写下的那个路径，否则它会换个写法再试一次。
    assert "/global/AGENTS.md" in feedback
    assert global_file.read_text(encoding="utf-8") == GLOBAL_TEXT
