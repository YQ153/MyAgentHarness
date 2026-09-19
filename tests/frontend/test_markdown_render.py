"""把前端 Markdown 渲染器的安全断言接进 pytest。

WHY 断言由 node 跑而不是在 Python 里复刻：这里要验的恰恰是「那一份实现是否安全」，
在 Python 里复刻一套等价的渲染逻辑，只会让断言通过在一个与产品无关的地方。

WHY 缺 node 时跳过而不是失败：渲染器只在浏览器里运行，把 node 变成跑 Python 测试的
硬依赖会挡住所有不碰前端的人；跳过是诚实的——它明确表示「这一格没验」，而不是伪装成绿。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests" / "frontend" / "test_markdown.mjs"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="未安装 node，跳过前端渲染器的安全用例")


def test_markdown_suite_passes() -> None:
    """跑 node 侧的用例，并要求它确实执行了足够多的用例。

    WHY 还要数一遍通过数：``node --test`` 在「一个用例都没收集到」时同样以 0 退出 ——
    只看退出码的话，用例文件被改名或写错路径都会表现为「安全测试全绿」。
    """
    assert SUITE.is_file(), f"用例文件不存在：{SUITE}"

    result = subprocess.run(
        [NODE, "--test", str(SUITE)],
        capture_output=True,
        text=True,
        # WHY 显式指定编码：node 的报告里带中文用例名，而 Windows 上 subprocess 默认按
        # locale（cp936）解码，会在读取线程里抛 UnicodeDecodeError——表现为本用例失败，
        # 且失败信息与「渲染器是否安全」毫无关系。errors="replace" 是兜底：即便上游输出
        # 里混进别的编码，也要把断言跑完再判，而不是在解码处中断。
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        timeout=180,
    )
    output = (result.stdout or "") + (result.stderr or "")

    assert result.returncode == 0, output

    passed = re.search(r"pass (\d+)", output)
    assert passed is not None, output
    assert int(passed.group(1)) >= 12, f"执行到的用例太少，疑似未被收集：\n{output}"
