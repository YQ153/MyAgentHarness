"""HarnessProfile 注册：把「模型差异」从装配代码里挪出去。

WHY 使用 Profile 而不是在 ``graph.py`` 里写 if-else：``create_deep_agent``
会在模型构造完成后按 provider 或 ``provider:model`` 查表并做加性合并，
新增模型只需在此注册一条，装配点零改动。
"""

from __future__ import annotations

import logging
import threading

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_REGISTERED = False

DEEPSEEK_PROFILE_KEY = "deepseek"
"""Provider 级 key；``register_harness_profile`` 接受 provider 名或 provider:model。"""

_BASE_SYSTEM_PROMPT = """你是通用任务助手，运行在受限沙箱工作区内。

## 工作方式

1. 超过 3 步的任务先用 write_todos 拆解计划，再逐步执行。
2. 工具返回的大结果（搜索命中、长文件内容、命令输出）先 write_file 落盘，
   只把结论与关键片段带回对话。
3. 一次性把事情做完，不要中途停下来询问是否可以继续。

## 工具使用约束

- 读取文件优先用 read_file 的 offset/limit 参数，避免整本灌入上下文。
- 修改已有文件用 edit_file，不要用 write_file 覆盖整个文件。
- 不要尝试读取 .env、密钥、证书类文件，这类访问会被安全规则直接拒绝。
"""

_SYSTEM_PROMPT_SUFFIX = "回答使用简体中文，代码与命令保持原样。"


def ensure_profiles_registered() -> None:
    """幂等地注册所有 HarnessProfile。

    WHY 加锁：Web 服务的工作线程可能同时首次调用装配路径，重复注册虽然被
    框架容忍，但会让日志重复且浪费一次合并计算。
    """
    global _REGISTERED

    if _REGISTERED:
        return

    with _LOCK:
        if _REGISTERED:
            return

        register_harness_profile(
            DEEPSEEK_PROFILE_KEY,
            HarnessProfile(
                base_system_prompt=_BASE_SYSTEM_PROMPT,
                system_prompt_suffix=_SYSTEM_PROMPT_SUFFIX,
                general_purpose_subagent=GeneralPurposeSubagentProfile(
                    enabled=True,
                    description=(
                        "处理需要多步探索、会产生大量中间结果的子任务，"
                        "例如批量检索代码、梳理文件结构、产出调查报告。"
                    ),
                ),
            ),
        )
        _REGISTERED = True
        logger.info("已注册 harness profile：%s", DEEPSEEK_PROFILE_KEY)
