"""配置文件里「不被任何字段读取的键」必须被点名。

WHY 需要这组用例：``extra="ignore"`` 让未知键静默失效——这是必要的（同一份 ``.env`` 常
混着别的工具的变量，判成错误会让升级直接起不来），但**一声不响**有代价，而且实测踩到了：
``.env`` 里残留的 ``WORKSPACE=./workspace``（旧模型的「默认工作空间」）读起来就是
「未绑定的会话会落到 ./workspace」，而应用根本不读它——用户据此以为「自动建目录」没生效，
排查方向被引到了完全错误的地方（2026-09-21）。

一句话：静默忽略可以，但得说出来。
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import AppConfig


def _write(tmp_path: Path, content: str) -> Path:
    """写一份临时 env 文件；数据目录也指到 ``tmp_path``，避免写到真实位置。"""
    env_file = tmp_path / "custom.env"
    env_file.write_text(content, encoding="utf-8")
    return env_file


def test_unknown_keys_are_named_in_the_warning(tmp_path: Path, caplog) -> None:
    """旧版本的残留键要被点名，已知键不要被牵连。"""
    env_file = _write(
        tmp_path,
        f"WORKSPACE=./workspace\nDB_PATH={tmp_path / 'agent.db'}\n",
    )

    with caplog.at_level(logging.WARNING, logger="config"):
        config = AppConfig.load(_env_file=env_file)

    assert config.warn_unknown_env_keys(env_file) == ["WORKSPACE"]
    assert "WORKSPACE" in caplog.text
    assert "不被任何字段读取" in caplog.text
    assert "DB_PATH" not in caplog.text, "已知键不该出现在这条告警里"


def test_a_lowercase_known_key_is_not_reported(tmp_path: Path, caplog) -> None:
    """大小写不敏感：``db_path`` 与 ``DB_PATH`` 等价，不该被当成未知键。

    WHY 单列：误报会把这条提示训练成噪音，而噪音很快就会被所有人忽略——那时它等于没有。
    """
    env_file = _write(tmp_path, f"db_path={tmp_path / 'agent.db'}\nauth_mode=disabled\n")

    with caplog.at_level(logging.WARNING, logger="config"):
        config = AppConfig(_env_file=env_file)

    assert config.warn_unknown_env_keys(env_file) == []
    assert "不被任何字段读取" not in caplog.text


def test_comments_and_blank_lines_are_not_keys(tmp_path: Path) -> None:
    """注释、空行、以及没有 ``=`` 的行都不算键。"""
    env_file = _write(
        tmp_path,
        "# WORKSPACE=./workspace\n\n   \nNOT_AN_ASSIGNMENT\n",
    )

    assert AppConfig(_env_file=env_file).warn_unknown_env_keys(env_file) == []


def test_a_missing_env_file_reports_nothing(tmp_path: Path) -> None:
    """文件不存在不是错误：没有配置文件是正常形态（默认值就够）。"""
    missing = tmp_path / "nope.env"

    assert AppConfig(_env_file=missing).warn_unknown_env_keys(missing) == []


def test_an_undecodable_env_file_is_reported_not_raised(tmp_path: Path, caplog) -> None:
    """读不动这份文件：告警并跳过检查，而不是让启动失败。

    WHY 这条取舍写下来：配置检查的价值远小于「因为一份编码坏掉的 .env 而拒绝启动」——
    后者会让用户在完全无关的方向上找问题（服务起不来，而原因是一行乱码）。
    """
    broken = tmp_path / "broken.env"
    broken.write_bytes(b"WORKSPACE=./workspace\n\xff\xfe\x00bad\n")
    config = AppConfig(_env_file=tmp_path / "missing.env")

    with caplog.at_level(logging.WARNING, logger="config"):
        unknown = config.warn_unknown_env_keys(broken)

    assert unknown == []
    assert "跳过未知键检查" in caplog.text
