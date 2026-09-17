"""``AppConfig`` 的 repr / str 不得带出任何密钥明文（技术债 D-3）。

WHY 需要这条回归：``repr(config)`` 会出现在三个我们控制不了的出口——pytest 断言
失败输出、日志、异常上报系统。只要密钥参与默认 repr，一次调试用的打印或一次断言
失败就会把明文写进 CI 日志（对协作者可见且有留存），而整个过程不报错、不告警，
没有任何迹象。D-3 的记录正是「已在一次 pytest 失败输出中实际观察到」。

为什么不屏蔽 ``model_dump()``：它按契约就是「取出字段值」，屏蔽会破坏真正需要读
配置的调用方。正确做法是确认没有人在日志/错误路径上用它——全仓检索确认三处调用
都作用于 DTO 与请求体，没有一处作用于 ``AppConfig``（见《第三阶段开发计划》T12）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import AppConfig
from tests.conftest import make_config

_SECRETS: dict[str, str] = {
    "deepseek_api_key": "sk-deepseek-PLAINTEXT-0001",
    "openai_api_key": "sk-openai-PLAINTEXT-0002",
    "anthropic_api_key": "sk-ant-PLAINTEXT-0003",
    "auth_session_secret": "session-PLAINTEXT-0004-0123456789abcdef",
    "auth_api_key_dev": "devkey-PLAINTEXT-0005",
    "oidc_client_secret": "oidc-PLAINTEXT-0006",
}
"""每个字段一个独特且可 grep 的明文哨兵，失败时能一眼看出是哪个字段漏了。"""


@pytest.fixture
def secret_config(tmp_path: Path) -> AppConfig:
    """字段值全部写入明文哨兵，用于断言这些哨兵不出现在任何展示文本中。"""
    return make_config(tmp_path, **_SECRETS)


@pytest.mark.parametrize("field", sorted(_SECRETS))
def test_repr_excludes_secret(secret_config: AppConfig, field: str) -> None:
    assert _SECRETS[field] not in repr(secret_config), f"{field} 的明文出现在 repr(config) 中"


@pytest.mark.parametrize("field", sorted(_SECRETS))
def test_str_excludes_secret(secret_config: AppConfig, field: str) -> None:
    """``str`` 与 ``repr`` 是两个入口，必须分别断言。

    WHY 分开测：只覆盖其中一个的话，另一个仍会把密钥交给 ``print(config)``
    或 f-string 插值——那正是「修了一半」的形态。
    """
    assert _SECRETS[field] not in str(secret_config), f"{field} 的明文出现在 str(config) 中"


def test_secrets_remain_readable(secret_config: AppConfig) -> None:
    """``repr=False`` 只应影响展示。

    WHY 必测：把「不打印」误修成「读不到」是更严重的回退——配置项静默失效，
    症状却是「密钥明明配了却说没配」，排查方向会完全跑偏到配置层。
    """
    for field, expected in _SECRETS.items():
        assert getattr(secret_config, field) == expected


def test_repr_still_shows_non_secret_fields(secret_config: AppConfig) -> None:
    """不能靠「把整个 repr 清空」来通过上面的断言。"""
    rendered = repr(secret_config)
    assert "deepseek-flash" in rendered  # default_model 默认值
    assert "127.0.0.1" in rendered  # host 默认值


def test_assertion_failure_output_does_not_leak(secret_config: AppConfig) -> None:
    """复现 D-3 的现场：断言失败时 pytest 打印的正是两侧对象的 repr。"""
    with pytest.raises(AssertionError) as excinfo:
        assert secret_config == "故意比较一个不相等的值"

    rendered = str(excinfo.value)
    for field, value in _SECRETS.items():
        assert value not in rendered, f"断言失败输出泄漏了 {field}"
