"""HITL 中断载荷编解码的回归测试。

覆盖面：中断提取（容器形态、非 HITL 适配）、审批决策规整
（四种类型、容错输入、非法输入）、恢复指令构造。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langgraph.types import Command

from application.interrupt_codec import (
    INTERRUPT_NODE,
    InterruptRequest,
    build_resume_command,
    decode_interrupt,
    normalize_decisions,
)


def _interrupt_payload() -> dict:
    return {
        "action_requests": [{"name": "execute", "args": {"command": "rm -rf /"}}],
        "review_configs": [{"action_name": "execute", "allowed_decisions": ["approve", "reject"]}],
    }


def _fake_interrupt(interrupt_id: str = "int-1", value: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=interrupt_id, value=value if value is not None else _interrupt_payload())


# ------------------------------------------------------------------ 解码


def test_decode_ignores_non_dict_update():
    assert decode_interrupt("messages") is None
    assert decode_interrupt(None) is None


def test_decode_ignores_plain_update():
    assert decode_interrupt({"agent": {"messages": []}}) is None


def test_decode_hitl_tuple_form():
    result = decode_interrupt({INTERRUPT_NODE: (_fake_interrupt(),)})

    assert isinstance(result, InterruptRequest)
    assert result.interrupt_id == "int-1"
    assert result.action_requests[0]["name"] == "execute"
    assert result.review_configs[0]["action_name"] == "execute"


def test_decode_hitl_list_form():
    result = decode_interrupt({INTERRUPT_NODE: [_fake_interrupt("int-2")]})

    assert result.interrupt_id == "int-2"


def test_decode_raw_object_without_container():
    result = decode_interrupt({INTERRUPT_NODE: _fake_interrupt("int-3")})

    assert result.interrupt_id == "int-3"
    assert result.action_requests[0]["name"] == "execute"


def test_decode_non_hitl_value_is_adapted():
    """非 HITL 形态（业务代码裸 interrupt）必须包成统一形状，前端只处理一种结构。"""
    result = decode_interrupt({INTERRUPT_NODE: (_fake_interrupt(value="需要确认"),)})

    assert result.action_requests[0]["description"] == "需要确认"
    assert result.review_configs[0]["allowed_decisions"] == ["approve", "reject"]


def test_to_payload_keys():
    result = decode_interrupt({INTERRUPT_NODE: (_fake_interrupt(),)})

    assert set(result.to_payload()) == {"interrupt_id", "action_requests", "review_configs"}


# ------------------------------------------------------------------ 决策规整


def test_normalize_approve_from_wrapped_form():
    assert normalize_decisions({"decisions": [{"type": "approve"}]}) == [{"type": "approve"}]


def test_normalize_approve_from_bare_dict():
    assert normalize_decisions({"type": "approve"}) == [{"type": "approve"}]


def test_normalize_approve_from_bare_list():
    assert normalize_decisions([{"type": "approve"}]) == [{"type": "approve"}]


def test_normalize_reject_includes_message():
    decisions = normalize_decisions(
        {"decisions": [{"type": "reject", "message": "危险操作"}]}
    )

    assert decisions == [{"type": "reject", "message": "危险操作"}]


def test_normalize_respond_without_message_defaults_to_empty():
    decisions = normalize_decisions({"decisions": [{"type": "respond"}]})

    assert decisions == [{"type": "respond", "message": ""}]


def test_normalize_edit_requires_name():
    with pytest.raises(ValueError):
        normalize_decisions(
            {"decisions": [{"type": "edit", "edited_action": {"args": {}}}]}
        )


def test_normalize_edit_keeps_args():
    decisions = normalize_decisions(
        {
            "decisions": [
                {
                    "type": "edit",
                    "edited_action": {"name": "execute", "args": {"command": "ls"}},
                }
            ]
        }
    )

    assert decisions == [
        {"type": "edit", "edited_action": {"name": "execute", "args": {"command": "ls"}}}
    ]


def test_normalize_unknown_type_rejected():
    with pytest.raises(ValueError):
        normalize_decisions({"decisions": [{"type": "maybe"}]})


@pytest.mark.parametrize(
    "raw",
    [
        "approve",
        None,
        [],
        [123],
        {"decisions": []},
        {"decisions": "approve"},
    ],
)
def test_normalize_invalid_payloads_rejected(raw):
    with pytest.raises(ValueError):
        normalize_decisions(raw)


# ------------------------------------------------------------------ 恢复指令


def test_build_resume_command_shape():
    command = build_resume_command({"decisions": [{"type": "approve"}]})

    assert isinstance(command, Command)
    # HITLResponse 的唯一合法形状：包一层的 decisions 列表
    assert command.resume == {"decisions": [{"type": "approve"}]}


def test_build_resume_command_rejects_invalid_payload():
    with pytest.raises(ValueError):
        build_resume_command({"decisions": [{"type": "nonsense"}]})
