"""执行档位与工具级文件权限的相容性测试。

WHY 必须单独覆盖：``EXECUTION_MODE=local / sandbox`` 曾因「工具级权限 + 可执行
backend」被 deepagents 直接拒绝而在装配阶段失败——这既挡住了执行能力，也让
「权限规则是否仍然生效」变成一件只能靠读代码判断的事。本文件把两条口径钉住：

1. 不可执行 backend（``disabled`` 档位）下，敏感路径拒绝必须照常生效；
2. 可执行 backend 下，工具级权限被**显式**停用（含 WARNING）且装配成功。

第 2 条同时是「``execute`` 的防护不能靠路径规则」这一事实的回归：一旦上游将来
改变约束，这里的正 / 负用例会立刻指出行为变化，而不是让防护在无声中失效。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from deepagents import create_deep_agent
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.store.memory import InMemoryStore

from agent.backends import build_backend
from agent.guardrails import build_interrupt_on, build_permissions
from agent.run_context import AgentRunContext
from tests.conftest import make_config, make_root


class ScriptedChatModel(BaseChatModel):
    """按脚本逐轮产出 ``AIMessage`` 的假模型（与记忆端到端用例同款）。

    WHY 自建而不复用共享夹具：deepagents 装配时会调 ``bind_tools``，通用假模型
    未必实现它；这里显式返回自身，把「模型行为」压缩成「第几轮返回哪条消息」。
    """

    replies: list[BaseMessage]

    @property
    def _llm_type(self) -> str:
        return "scripted-permissions"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        index = sum(1 for message in messages if isinstance(message, AIMessage))
        reply = self.replies[min(index, len(self.replies) - 1)]
        return ChatResult(generations=[ChatGeneration(message=reply)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)


def _tool_call(name: str, args: dict[str, Any], call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _config(tmp_path: Path, **overrides: Any):
    """构造配置并建好工作区目录（``build_backend`` 要求工作区存在）。"""
    config = make_config(tmp_path, **overrides)
    config.ensure_directories()
    return config


def _tool_outputs(chunks: list[Any]) -> list[str]:
    """从流分片里取出全部工具结果文本。"""
    outputs: list[str] = []
    for mode, payload in chunks:
        if mode != "messages":
            continue
        message, _meta = payload
        if type(message).__name__ == "ToolMessage":
            outputs.append(str(message.content))
    return outputs


# ---------------------------------------------------------------- 规则裁剪口径


def test_disabled_backend_keeps_tool_level_permissions(tmp_path: Path):
    """``disabled`` 档位下敏感路径拒绝必须保留——这是文件工具的主防线。"""
    config = _config(tmp_path)
    backend = build_backend(config, InMemoryStore(), scope=make_root(config))

    rules = build_permissions(backend)

    assert rules, "不可执行 backend 下不应丢弃工具级权限"
    assert any(rule.mode == "deny" for rule in rules)


def test_local_backend_drops_tool_level_permissions(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """``local`` 档位下工具级权限被停用，且必须留下 WARNING（不静默）。"""
    config = _config(tmp_path, execution_mode="local")
    backend = build_backend(config, InMemoryStore(), scope=make_root(config))

    with caplog.at_level(logging.WARNING):
        rules = build_permissions(backend)

    assert rules == [], "可执行 backend 下必须返回空规则，否则 deepagents 会拒绝装配"
    assert "命令执行能力" in caplog.text


def test_sandbox_backend_drops_tool_level_permissions(tmp_path: Path):
    """``sandbox`` 档位与 ``local`` 同口径：可执行就不带工具级权限。"""
    config = _config(tmp_path, execution_mode="sandbox", sandbox_tier="process")
    backend = build_backend(config, InMemoryStore(), scope=make_root(config))

    assert build_permissions(backend) == []


def test_parameterless_default_returns_full_rules():
    """不传 backend 时按「不执行命令」处理，保留完整规则。

    WHY 要钉住：``build_permissions()`` 的无参调用是配置校验等不涉及装配的调用方
    的入口；若它悄悄变成空规则，这些路径上的敏感文件保护会一起失效。
    """
    rules = build_permissions()

    assert len(rules) == 3
    assert rules[0].mode == "deny"


# ------------------------------------------------------ 上游约束与装配可行性


def test_full_rules_rejected_on_executable_backend(tmp_path: Path):
    """复刻上游约束：可执行 backend + 完整权限规则 → ``NotImplementedError``。

    WHY 保留这条「负用例」：它是「必须裁剪」这一结论的前提。前提消失（上游开始
    支持该组合）时本用例会转红，提示我们重新评估是否还要停用权限。
    """
    config = _config(tmp_path, execution_mode="local")
    backend = build_backend(config, InMemoryStore(), scope=make_root(config))

    with pytest.raises(NotImplementedError):
        FilesystemMiddleware(backend=backend, _permissions=build_permissions())


def test_local_mode_graph_assembles(tmp_path: Path):
    """真图装配回归：``local`` 档位不再是「装不出来」的档位。

    这是本任务的核心验收——把与 ``build_agent`` 相同的参数组合交给
    ``create_deep_agent``，装配必须成功。
    """
    config = _config(tmp_path, execution_mode="local")
    store = InMemoryStore()
    backend = build_backend(config, store, scope=make_root(config))

    agent = create_deep_agent(
        model=ScriptedChatModel(replies=[AIMessage(content="ok")]),
        backend=backend,
        store=store,
        # 与 agent/graph.py 的接线保持一致，避免用例验证的是另一套参数
        permissions=build_permissions(backend),
        interrupt_on=build_interrupt_on(
            config.execution_mode,
            config.sandbox_tier,
            require_approval=config.sandbox_require_approval,
        ),
        context_schema=AgentRunContext,
        name="permissions-local",
    )

    assert agent is not None


# --------------------------------------------------------- 端到端：权限仍生效


async def test_disabled_mode_denies_secret_file_read_end_to_end(tmp_path: Path):
    """``disabled`` 档位端到端：读工作区内的 ``.env`` 被拒绝。

    WHY 走真图而不是直接调 ``_check_fs_permission``：要证明的是「规则真的接进了
    工具调用路径」，而不是「匹配函数算对了」——两者之间隔着装配这一层。
    """
    config = _config(tmp_path)
    (make_root(config).root / ".env").write_text("SECRET_VALUE=1\n", encoding="utf-8")
    store = InMemoryStore()
    backend = build_backend(config, store, scope=make_root(config))

    agent = create_deep_agent(
        model=ScriptedChatModel(
            replies=[
                _tool_call("read_file", {"file_path": "/.env"}),
                AIMessage(content="已尝试读取。"),
            ]
        ),
        backend=backend,
        store=store,
        permissions=build_permissions(backend),
        context_schema=AgentRunContext,
        name="permissions-disabled",
    )

    chunks = [
        chunk
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": "读一下 .env"}]},
            config={"configurable": {"thread_id": "perm-1"}, "recursion_limit": 50},
            stream_mode=["messages", "updates"],
            context=AgentRunContext(user_id="alice"),
        )
    ]

    joined = "\n".join(_tool_outputs(chunks))
    assert "permission denied" in joined
    assert "SECRET_VALUE=1" not in joined
