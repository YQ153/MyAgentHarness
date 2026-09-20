"""文本规整的边界契约。

WHY 单独成文件：`text_utils` 是被 `application` 与 `runtime` 同时依赖的中立模块
（见其模块 docstring），此前只被间接覆盖——经会话标题、经工具输出。而间接覆盖
发现不了"某一侧的措辞被换掉"或"某一侧的上限偷偷失效"这类改动。

WHY 重点钉长度口径而不是钉实现：这两个截断函数的分歧点全在长度上——
`truncate` 面向展示，省略号占一个字符，结果总长**不超过** limit；
`truncate_with_notice` 面向工具输出，保留 limit 个字符后**再**追加提示语，
总长**可以超过** limit。差一位看不出差别，但会让"输出上限"在某一侧静默失效；
合并两者则会让其中一侧的行为整体改变（见方案二对这次合并的否决记录）。
"""

from __future__ import annotations

import pytest

from text_utils import build_title, collapse_whitespace, truncate, truncate_with_notice

_NOTICE = "…(truncated)"


def test_truncate_keeps_total_length_within_limit() -> None:
    """展示口径：结果总长不超过上限，省略号占掉其中一位。"""
    assert truncate("abcdef", 4) == "abc…"
    assert len(truncate("abcdef", 4)) == 4


def test_truncate_returns_text_unchanged_when_within_limit() -> None:
    """恰好等于上限时不截断——边界差一位是最容易写错的比较。"""
    assert truncate("abcd", 4) == "abcd"
    assert truncate("ab", 4) == "ab"


def test_truncate_with_notice_appends_only_when_content_is_cut() -> None:
    """未超限必须原样返回。

    WHY 这条值得单独钉：无条件追加提示语会让**完整**的结果自称被截断了，
    读者据此丢掉后半段本可用的信息——而这正是这个函数存在的理由
    （让"截断"这件事可见，而不是让所有结果都像截断）。
    """
    assert truncate_with_notice("abc", 3, _NOTICE) == "abc"
    assert truncate_with_notice("ab", 3, _NOTICE) == "ab"
    assert truncate_with_notice("abcd", 3, _NOTICE) == "abc" + _NOTICE


def test_the_two_truncation_policies_stay_distinct() -> None:
    """两份截断不能合并：长度口径相反，合并必然改变某一侧的行为。

    WHY 用断言把差异写出来：后来者看到两个相似函数时，最自然的动作是"合并省事"。
    这条测试让那次合并立刻失败，并把"为什么不能合"写在断言里，而不是等它
    在工具输出长度或标题显示上表现出来。
    """
    text = "0123456789"
    limit = 4

    display = truncate(text, limit)
    tool = truncate_with_notice(text, limit, _NOTICE)

    assert len(display) <= limit, "展示口径：总长不超过上限"
    assert tool.startswith(text[:limit]), "工具口径：先保留 limit 个字符"
    assert len(tool) > limit, "工具口径：提示语独立追加，总长可超上限"
    assert display != tool


@pytest.mark.parametrize(
    ("text", "limit", "notice"),
    [
        ("abc", 0, _NOTICE),  # 上限小于 1：切不出任何内容
        ("abc", -1, _NOTICE),
        ("abc", True, _NOTICE),  # bool 是 int 的子类，必须显式排除
        ("abc", "3", _NOTICE),  # 类型错误
        ("abc", 3, ""),  # 空提示语等于静默截断，与函数存在的理由相悖
        ("abc", 3, None),
        (None, 3, _NOTICE),  # 输入来自上游解析，可能是 None
    ],
)
def test_truncate_with_notice_rejects_invalid_arguments(
    text: object, limit: object, notice: object
) -> None:
    with pytest.raises(ValueError):
        truncate_with_notice(text, limit, notice)  # type: ignore[arg-type]


def test_truncate_rejects_non_string_text() -> None:
    """两个截断函数的入参校验必须一致，否则读代码的人要分别记住各自的口径。"""
    with pytest.raises(ValueError, match="text 必须是字符串"):
        truncate(None, 5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit 必须 >= 1"):
        truncate("abc", 0)


def test_build_title_collapses_whitespace_and_respects_limit() -> None:
    """标题是 `truncate` 的既有调用方，它同时承担折叠与截断两件事。"""
    assert build_title("  多   空白\n标题  ", 24) == "多 空白 标题"
    assert len(build_title("x" * 100, 10)) == 10
    assert build_title("   ", 10) == ""
    assert build_title(None, 10) == ""


def test_collapse_whitespace_normalises_any_input() -> None:
    """折叠要能吃下非字符串：标题正文来自模型输出，类型不总是可靠的。"""
    assert collapse_whitespace("  a\n\tb  ") == "a b"
    assert collapse_whitespace(None) == ""
    assert collapse_whitespace(123) == "123"
