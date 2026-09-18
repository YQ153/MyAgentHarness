"""`.env.example` 与 `config.py` 的口径契约。

WHY 需要这条断言：模板与配置字段一旦漂移，症状只有两种，且**都不会让任何测试变红**——
「照着模板配了却不起作用」（模板里是已删除的参数），或「配置里有的参数无处可查」
（模板漏了它，只能去读源码）。这两种都是部署之后才发现，靠人眼比对两个文件又必然漏。
"""

from __future__ import annotations

import re
from pathlib import Path

from config import AppConfig

ROOT = Path(__file__).resolve().parents[1]
KEY_LINE = re.compile(r"^#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _model_fields() -> set[str]:
    """配置字段的环境变量名（本项目没有 env_prefix，故只差大小写）。"""
    return {name.upper() for name in AppConfig.model_fields}


def _documented_keys() -> set[str]:
    """模板里出现过的键；被注释的也算已文档化（示例里大量参数就是注释形态）。"""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    keys: set[str] = set()
    for line in text.splitlines():
        match = KEY_LINE.match(line.strip())
        if match:
            keys.add(match.group(1).upper())
    return keys


def test_template_has_no_unknown_keys() -> None:
    unknown = sorted(_documented_keys() - _model_fields())

    assert not unknown, (
        f".env.example 里有 config 未定义的键（陈旧或拼错，照它配置不会生效）：{unknown}"
    )


def test_every_config_field_is_documented() -> None:
    missing = sorted(_model_fields() - _documented_keys())

    assert not missing, (
        f"这些配置字段在 .env.example 里完全没有出现，用户无从知道它们存在：{missing}"
    )
