"""会话 ID 校验的应用层门面。

WHY 需要门面而非直接引用：``normalize_thread_id`` 的实现在 ``runtime.thread_store``，
因为存储层也要用它做入参校验（规则必须只有一份）。但 ``interfaces`` 层直接引用
``runtime`` 会破坏 ``interfaces → application`` 的单向依赖，因此由本模块重新导出，
把依赖方向收束到合规路径上。

真正的规则实现仍留在 ``runtime``：它同时被存储层、服务层与接口层使用，
属于跨层的公共校验，迁移到 application 会让 ``runtime`` 反向依赖 ``application``。
"""

from __future__ import annotations

from runtime.thread_store import normalize_thread_id

__all__ = ["normalize_thread_id"]
