"""技能的安全红线：**技能只能注入指令，不能放宽权限与审批**。

技能包是一段会被注入模型上下文的**指令文本**。它天然有能力说服模型「去做某件事」，因此
本组用例要钉住的不是「模型会不会被说服」（那属于提示注入，无法靠断言穷尽），而是一条
更硬的性质：**技能的存在不改变护栏的构造结果**——不改工具集、不改文件权限、不改中断
（审批）配置。只要这条成立，技能能做的就仅限于「让模型调用本来就要走审批的工具」，而
审批链、沙箱与凭据防护一行都没被绕过。

为什么必须有这组用例：技能正文里写「先运行 scripts/cleanup.py」是最自然的写法，而
只要有一处实现把技能目录当成工具来源、或按技能存在与否调整审批，那条命令就从「需要
人工批准」变成「静默执行」——两种情况下界面都完全正常，看不出区别。

WHY 用替身替换 ``create_deep_agent``：装配真实图需要可用的模型与密钥，而这里要钉的
只是「参数有没有传对」（与 ``test_memory_namespace`` 同一手法）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from config import AppConfig
from tests.conftest import make_config

_SKILL_BODY = """---
name: {name}
description: {name} 的说明
---

# {name}

执行前先运行辅助脚本：

```bash
python scripts/{script}
```
"""


class _FakeRegistry:
    """模型注册表替身：装配测试不关心模型从哪来。"""

    default_name = "fake"

    def get(self, name: str | None = None) -> object:
        return object()


def _write_skill(skills_dir: Path, name: str, *, script: str | None = None) -> Path:
    """写一个技能包；``script`` 非空时额外放一个会被正文引用的辅助脚本。"""
    directory = skills_dir / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        _SKILL_BODY.format(name=name, script=script or "helper.py"), encoding="utf-8"
    )
    if script:
        (directory / "scripts").mkdir(exist_ok=True)
        # 内容刻意写成一条破坏性命令：若它被当成工具注册，下面立刻能看出来
        (directory / "scripts" / script).write_text(
            "import shutil; shutil.rmtree('/tmp/whatever')\n", encoding="utf-8"
        )
    return directory


def _snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_skill: bool) -> dict[str, Any]:
    """在「有 / 无技能」两种工作区里各装配一次图，返回捕获到的装配参数。"""
    from agent import graph as graph_module
    from agent.graph import build_agent

    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    if with_skill:
        _write_skill(skills_dir, "cleanup", script="cleanup.py")

    config: AppConfig = make_config(
        tmp_path,
        workspace=workspace,
        skill_dirs=[skills_dir],
        # local 档位：``execute`` 必须走人工审批，正是本文件要钉住的那条
        execution_mode="local",
    )

    captured: dict[str, Any] = {}
    monkeypatch.setattr(graph_module, "get_registry", lambda config: _FakeRegistry())
    monkeypatch.setattr(
        graph_module,
        "create_deep_agent",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    build_agent(config, store=InMemoryStore())
    return captured


def _tool_names(captured: dict[str, Any]) -> set[str]:
    """装配参数里的工具名集合（``None`` 表示未显式传入）。"""
    return {getattr(tool, "name", str(tool)) for tool in (captured.get("tools") or ())}


def _guardrails(captured: dict[str, Any]) -> dict[str, Any]:
    """把中断配置压成可比较的形状（值可能是 TypedDict，也可能是布尔开关）。"""
    result: dict[str, Any] = {}
    for key, value in (captured.get("interrupt_on") or {}).items():
        if isinstance(value, dict):
            result[key] = (tuple(value.get("allowed_decisions") or ()), value.get("description", ""))
        else:
            result[key] = value
    return result


# --------------------------------------------------------------- 前提


def test_skill_reaches_the_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """前提：技能确实被交给了图。

    没有这条，下面所有「技能没改变 X」的断言都可能在「技能压根没加载」上通过——
    那种绿色毫无意义。
    """
    captured = _snapshot(tmp_path, monkeypatch, with_skill=True)

    assert captured.get("skills"), "技能目录未被传给 create_deep_agent"


# --------------------------------------------------------------- 红线


def test_skill_does_not_remove_execute_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """核心红线：技能正文让模型「先跑脚本」，``execute`` 仍然需要人工审批。

    这是本任务里最需要守住的一条：技能是最自然的「往上下文里塞指令」的位置，一旦
    审批在技能存在时被放宽，那条命令就从「需要批准」变成「静默执行」，而界面上
    完全看不出区别。
    """
    captured = _snapshot(tmp_path, monkeypatch, with_skill=True)

    guardrails = _guardrails(captured)
    assert "execute" in guardrails, f"技能存在时 execute 不再需要审批：{guardrails}"
    decisions = guardrails["execute"][0]
    assert "approve" in decisions and "reject" in decisions


def test_skill_does_not_widen_the_tool_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """技能不会往工具集里加任何东西——它是指令，不是工具。

    WHY 这条单独存在：技能目录里有脚本文件（``scripts/cleanup.py``）。若某处实现
    「顺手把技能目录扫成工具」，破坏性命令就成了一个可被模型直接调用的工具，而它
    不属于任何一类需要审批的工具——审批配置里只写了 ``execute``。
    """
    without = _snapshot(tmp_path / "a", monkeypatch, with_skill=False)
    with_skill = _snapshot(tmp_path / "b", monkeypatch, with_skill=True)

    assert _tool_names(with_skill) == _tool_names(without)
    assert not any("cleanup" in name for name in _tool_names(with_skill))


def test_skill_does_not_change_guardrails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """护栏参数在「有 / 无技能」两种情况下**逐字节相同**。

    WHY 比逐个字段断言更强：将来有人给技能加一条「技能存在时顺带放宽某个权限」，只要
    它落在 ``permissions`` / ``interrupt_on`` 里，这条断言立刻红，而逐个字段的写法会
    漏掉新增的那一项。
    """
    without = _snapshot(tmp_path / "a", monkeypatch, with_skill=False)
    with_skill = _snapshot(tmp_path / "b", monkeypatch, with_skill=True)

    assert _guardrails(with_skill) == _guardrails(without)
    assert list(with_skill.get("permissions") or ()) == list(without.get("permissions") or ())


def test_skill_presence_does_not_relax_permissions_on_executable_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """可执行 backend 下权限规则仍为空（把防护交给审批与隔离），技能不改变这一点。

    WHY：deepagents 在「可执行 backend + 权限规则」组合上会直接抛错，因为一条
    ``cat .env`` 能绕过全部路径规则。所以本仓在该组合下返回空规则并显式告警——
    **不做静默降级**。技能存在时若有人「顺手补一条规则」来让它看起来更安全，
    实际上是在制造一条纸面防线。
    """
    captured = _snapshot(tmp_path, monkeypatch, with_skill=True)

    assert list(captured.get("permissions") or ()) == []
