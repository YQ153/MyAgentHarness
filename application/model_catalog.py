"""模型目录：可切换模型的只读清单。

只承载「有哪些模型可用」这一件事，不参与会话与运行的任何编排。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from application.dto import ModelInfo

if TYPE_CHECKING:
    from llm.registry import ModelConfigProbe, ModelRegistry

logger = logging.getLogger(__name__)


class ModelCatalog:
    """对外暴露模型清单。

    WHY 从会话服务中分出来：模型目录与会话生命周期毫无关系。此前会话服务为了
    转发 ``registry.describe()`` 这样一行调用而持有整个注册表，属于典型的
    「为了转发而持有依赖」——它既不是会话的读写者，也不是运行的使用者。
    """

    def __init__(self, registry: ModelRegistry) -> None:
        """构造目录。

        Args:
            registry: 模型注册表。

        Raises:
            ValueError: ``registry`` 为 ``None``。
        """
        if registry is None:
            raise ValueError("registry 不能为 None")

        self._registry = registry

    def list_models(self) -> list[ModelInfo]:
        """列出可切换的模型，不含任何密钥信息。

        Returns:
            按别名升序排列的模型列表。
        """
        return [ModelInfo(**item) for item in self._registry.describe()]

    def has(self, name: str) -> bool:
        """判断某个模型别名是否已注册。

        Args:
            name: 模型别名。

        Returns:
            已注册返回 ``True``。
        """
        if not isinstance(name, str) or not name:
            return False
        return name in set(self._registry.names())

    def probe(self, name: str | None = None) -> ModelConfigProbe:
        """探测某个模型别名的配置是否可用于发起请求。

        WHY 经由目录转发而不是让调用方直接持有注册表：注册表是 ``llm`` 层的
        实现细节，目录才是应用层对模型能力的门面；这样探测逻辑只存在于一处，
        调用方也无需为一次只读探测引入对 ``llm`` 的依赖。

        Args:
            name: 模型别名；``None`` 表示探测默认模型。

        Returns:
            探测结果，含是否可用与不可用原因。
        """
        return self._registry.probe_config(name)
