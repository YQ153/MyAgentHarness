"""模型层：多 provider 注册与切换。

对外只暴露 ``ModelRegistry`` 与 ``ModelSpec``，装配层不需要感知 provider 差异。
"""

from llm.registry import ModelRegistry, ModelSpec, build_default_registry

__all__ = ["ModelRegistry", "ModelSpec", "build_default_registry"]
