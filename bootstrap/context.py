"""已装配依赖的不可变集合。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from agent.graph import AgentFactory
    from application.health import HealthService
    from application.memory_service import MemoryService
    from application.model_catalog import ModelCatalog
    from application.run_service import RunService
    from application.thread_service import ThreadService
    from application.tool_catalog import ToolCatalog
    from application.usage_service import UsageService
    from application.workspace_service import WorkspaceService
    from config import AppConfig
    from runtime.api_key_store import APIKeyStore
    from runtime.audit_store import AuditStore
    from runtime.device_flow_store import DeviceFlowStore
    from runtime.thread_store import ThreadMetaStore
    from runtime.usage_store import UsageStore


@dataclass(frozen=True)
class AppContext:
    """应用级已装配依赖集合。

    WHY 不可变：装配阶段结束后，任何依赖都不应再被替换。可变上下文会让并发
    请求看到「半新半旧」的依赖组合——例如部分请求用旧 checkpointer、部分用新
    checkpointer——这类问题只在竞态路径下暴露，极难复现。

    Attributes:
        config: 应用配置。
        checkpointer: LangGraph 检查点持久化实现。
        thread_store: 会话元数据存储。
        audit_store: 审计日志存储。
        api_key_store: API Key 存储。
        device_flow_store: CLI 设备授权流程状态存储。
        graph_factory: 按模型别名提供已装配图的工厂。
        threads: 会话元数据服务。
        runs: 运行推进服务。
        catalog: 可切换模型的只读目录。
        health: 健康检查与运行指标服务。
        usage_store: Token 用量存储。
        usage: 用量统计服务。
        tools: 生效工具目录（内置 + 自定义 + MCP）。
        memories: 长期记忆管理服务（查看 / 删除）。
        workspace: 工作区文件服务（Web 文件面板的列目录与读文件）。
    """

    config: AppConfig
    checkpointer: BaseCheckpointSaver
    thread_store: ThreadMetaStore
    audit_store: AuditStore
    api_key_store: APIKeyStore
    device_flow_store: DeviceFlowStore
    graph_factory: AgentFactory
    threads: ThreadService
    runs: RunService
    catalog: ModelCatalog
    health: HealthService
    usage_store: UsageStore
    usage: UsageService
    tools: ToolCatalog
    memories: MemoryService
    workspace: WorkspaceService
