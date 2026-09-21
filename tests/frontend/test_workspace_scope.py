"""把工作区「根作用域」的前端断言接进 pytest。

WHY 断言由 node 跑而不是在 Python 里复刻：要验的是 ``workspace_scope.js`` **那一份实现**
有没有把换会话时的四份界面缓存清干净；在 Python 里复刻一套等价逻辑，只会让断言通过在一个
与产品无关的地方（与 markdown / 凭据两支同一取舍）。

WHY 缺 node 时跳过而不是失败：前端脚本只在浏览器里运行，把 node 变成跑 Python 测试的硬
依赖会挡住所有不碰前端的人。

WHY 显式指定 ``encoding="utf-8"``：node 的测试报告里带中文用例名，而 Windows 上
``subprocess`` 默认按 locale（cp936）解码，会直接抛 ``UnicodeDecodeError``——表现为
「前端用例全挂」而实际一行断言都没跑（markdown 那支用例曾因此长期失败）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests" / "frontend" / "test_workspace_scope.mjs"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="未安装 node，跳过前端工作区作用域用例")

_MIN_CASES = 10
"""期望至少跑到的用例数。

WHY 要数一遍：``node --test`` 在「一个用例都没收集到」时同样以 0 退出——只看退出码的话，
用例文件被改名或路径写错都会表现为「换根清理全部通过」。
"""


def test_workspace_scope_suite_passes() -> None:
    """跑 node 侧的工作区作用域用例，并要求它确实执行了足够多的用例。"""
    assert SUITE.is_file(), f"用例文件不存在：{SUITE}"

    result = subprocess.run(
        [NODE, "--test", str(SUITE)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        timeout=180,
    )
    output = (result.stdout or "") + (result.stderr or "")

    assert result.returncode == 0, output

    passed = re.search(r"pass (\d+)", output)
    assert passed is not None, output
    assert int(passed.group(1)) >= _MIN_CASES, f"执行到的用例太少，疑似未被收集：\n{output}"
