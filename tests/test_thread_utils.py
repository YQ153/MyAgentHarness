"""会话 ID 校验规则的回归测试。

覆盖面：首尾空白折叠、空串、非字符串、超长，以及恰好等于上限的边界值。
"""

from __future__ import annotations

import pytest

from thread_utils import MAX_THREAD_ID_CHARS, normalize_thread_id


def test_normalize_strips_whitespace():
    assert normalize_thread_id("  abc  ") == "abc"


def test_normalize_rejects_empty():
    with pytest.raises(ValueError):
        normalize_thread_id("   ")


def test_normalize_rejects_non_string():
    with pytest.raises(ValueError):
        normalize_thread_id(12345)


def test_normalize_rejects_overlong():
    with pytest.raises(ValueError):
        normalize_thread_id("x" * (MAX_THREAD_ID_CHARS + 1))


def test_normalize_accepts_max_length():
    assert normalize_thread_id("x" * MAX_THREAD_ID_CHARS) == "x" * MAX_THREAD_ID_CHARS
