"""真机冒烟：三种执行档位的装配与命令执行。

背景：``EXECUTION_MODE=local / sandbox`` 曾因 deepagents 拒绝「工具级权限 + 可执行
backend」而在装配阶段直接失败（详见 ``scripts/probe_permissions_backend.py``）。
本脚本在真实宿主上把修复后的行为跑一遍，回答三个问题：

1. ``disabled`` 档位仍返回完整的工具级权限规则（敏感路径拒绝不能丢）；
2. ``local`` / ``sandbox`` 档位能装配出真图，且 ``execute`` 真的跑在宿主 / 沙箱上；
3. 若本机配了真实模型密钥，``agent.graph.build_agent`` 这条生产入口在 ``local``
   档位下也能装配成功（不发起模型调用）。

WHY 用脚本化模型而不是真模型：本脚本要验证的是「执行链路是否通」，而不是模型
会不会调用工具——真模型会把「链路断了」和「模型没按预期调用」混成同一个失败。
沙箱档位取 ``process``（Tier 0）以保证跨平台可复现；``wsl`` / ``docker`` 需要
相应的宿主能力，不属于本脚本的验证范围。

WHY 不传 ``interrupt_on``：审批链路由 ``agent.guardrails.build_interrupt_on``
与既有用例覆盖；这里若挂上审批，图会在中断处停下等待人工决策，冒烟脚本无法无人
值守地跑完。因此本脚本验证的是审批之后的执行段。

退出码：``0`` 全部通过；``1`` 有失败项（含预期外异常）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
# WHY 强制 UTF-8 输出：Windows 控制台默认 GBK，命令输出含非 GBK 字符时 print
# 会抛 UnicodeEncodeError，把冒烟结论变成一条假失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from deepagents import create_deep_agent  # noqa: E402
from deepagents.middleware.filesystem import supports_execution  # noqa: E402
from langchain_core.callbacks import CallbackManagerForLLMRun  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402

from agent.backends import build_backend  # noqa: E402
from agent.guardrails import build_permissions  # noqa: E402
from agent.run_context import AgentRunContext  # noqa: E402
from config import AppConfig  # noqa: E402

logger = logging.getLogger("smoke.execution_modes")

_MARKER = "LOCAL_EXEC_MARKER_OK"


class _ScriptedChatModel(BaseChatModel):
    """先调用一次 ``execute``，再给出收尾回复的假模型。"""

    command: str

    @property
    def _llm_type(self) -> str:
        return "scripted-smoke"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        already_called = any(getattr(message, "tool_calls", None) for message in messages)
        if already_called:
            reply: BaseMessage = AIMessage(content="已完成。")
        else:
            reply = AIMessage(
                content="",
                tool_calls=[
                    {"name": "execute", "args": {"command": self.command}, "id": "call-1", "type": "tool_call"}
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=reply)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)


@dataclass
class _Result:
    name: str
    passed: bool
    detail: str


def _config(workdir: pathlib.Path, **overrides: Any) -> AppConfig:
    """构造落在临时目录里的配置，避免冒烟污染真实数据目录。"""
    params: dict[str, Any] = {
        "_env_file": workdir / "none.env",
        "auth_mode": "disabled",
        "memory_file": workdir / "AGENTS.md",
        "db_path": workdir / "agent.db",
        "skill_dirs": [workdir / "skills"],
    }
    params.update(overrides)
    config = AppConfig(**params)
    config.ensure_directories()
    return config


async def _run_execute(config: AppConfig, command: str, thread_id: str) -> str:
    """装配真图并让脚本化模型调用一次 ``execute``，返回工具输出拼接文本。"""
    store = InMemoryStore()
    backend = build_backend(config, store)
    agent = create_deep_agent(
        model=_ScriptedChatModel(command=command),
        backend=backend,
        store=store,
        permissions=build_permissions(backend),
        context_schema=AgentRunContext,
        name=f"smoke-{thread_id}",
    )
    chunks = [
        chunk
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": "执行命令"}]},
            config={"configurable": {"thread_id": thread_id}, "recursion_limit": 25},
            stream_mode=["messages", "updates"],
            context=AgentRunContext(user_id="smoke"),
        )
    ]
    outputs: list[str] = []
    for mode, payload in chunks:
        if mode != "messages":
            continue
        message, _meta = payload
        if type(message).__name__ == "ToolMessage":
            outputs.append(str(message.content))
    return "\n".join(outputs)


def _check_disabled(workdir: pathlib.Path) -> _Result:
    """``disabled`` 档位：权限规则必须保留。"""
    config = _config(workdir, execution_mode="disabled")
    backend = build_backend(config, InMemoryStore())
    rules = build_permissions(backend)
    if supports_execution(backend):
        return _Result("disabled 档位", False, "backend 不应具备执行能力")
    if len(rules) != 3 or not any(rule.mode == "deny" for rule in rules):
        return _Result("disabled 档位", False, f"权限规则被意外裁剪：{len(rules)} 条")
    return _Result("disabled 档位", True, f"执行能力关闭，工具级权限保留 {len(rules)} 条")


async def _check_executable(name: str, workdir: pathlib.Path, **overrides: Any) -> _Result:
    """``local`` / ``sandbox`` 档位：装配真图并真的跑一条命令。"""
    config = _config(workdir, **overrides)
    store = InMemoryStore()
    backend = build_backend(config, store)
    if not supports_execution(backend):
        return _Result(f"{name} 档位", False, "backend 未提供执行能力")

    rules = build_permissions(backend)
    if rules:
        return _Result(f"{name} 档位", False, f"可执行 backend 下仍带权限规则：{len(rules)} 条")

    output = await _run_execute(config, f"echo {_MARKER}", thread_id=f"smoke-{name}")
    if _MARKER not in output:
        return _Result(f"{name} 档位", False, f"命令输出未见标记，实际输出：{output[:200]!r}")
    return _Result(f"{name} 档位", True, f"装配成功且 execute 返回标记；输出={output.strip()[:60]!r}")


def _check_build_agent(workdir: pathlib.Path) -> _Result:
    """生产入口 ``build_agent`` 在 ``local`` 档位下的装配（需真实模型密钥）。"""
    # WHY 走 ``load()``：工作区必填，直接构造在未配置时只会抛 pydantic 原文；
    # ``load()`` 给出可照做的提示，并顺带把目录建好。
    base = AppConfig.load(_env_file=str(ROOT / ".env") if (ROOT / ".env").exists() else None)
    if not base.deepseek_api_key or base.deepseek_api_key.startswith("your"):
        return _Result("build_agent 装配", True, "SKIP 未配置真实模型密钥（不影响本地能力结论）")

    from agent.graph import build_agent  # noqa: PLC0415 - 仅此分支需要

    config = _config(workdir, execution_mode="local")
    config = config.model_copy(update={"deepseek_api_key": base.deepseek_api_key})
    try:
        agent = build_agent(config, store=InMemoryStore())
    except Exception as exc:  # noqa: BLE001 - 冒烟要把任何装配失败如实暴露
        return _Result("build_agent 装配", False, f"装配失败：{type(exc).__name__}: {exc}")
    return _Result("build_agent 装配", True, f"local 档位下生产入口装配成功：{type(agent).__name__}")


async def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    results: list[_Result] = []

    with tempfile.TemporaryDirectory() as tmp:
        workdir = pathlib.Path(tmp)
        results.append(_check_disabled(workdir))
        results.append(await _check_executable("local", workdir, execution_mode="local"))
        results.append(
            await _check_executable(
                "sandbox(process)",
                workdir,
                execution_mode="sandbox",
                sandbox_tier="process",
            )
        )
        results.append(_check_build_agent(workdir))

    print("\n=== 执行档位冒烟结果 ===")
    for item in results:
        print(f"[{'PASS' if item.passed else 'FAIL'}] {item.name}: {item.detail}")

    return 0 if all(item.passed for item in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
