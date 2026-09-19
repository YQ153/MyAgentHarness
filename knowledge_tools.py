"""知识库自定义工具：让 Agent 自主检索并索引工作区文档。

启用方式：``CUSTOM_TOOL_MODULES=knowledge_tools``

WHY 做成自定义工具而不是内置工具：知识库是可选能力（要装 ``sqlite-vec``、可能要配
嵌入后端），塞进内置工具集会让没启用它的部署也背上一段工具描述与失败面；T8 的扩展点
正是为这类「按配置启用」的能力准备的。

WHY 工具体内按需取服务而不是注册时注入：扩展点只把 ``config`` 交给模块，而知识库
持有的是一条 SQLite 连接。``knowledge_runtime`` 负责「整进程一份」，这里只取用。

WHY 检索结果要截断：一次返回若干个片段，每个都可能上千字；原样返回会把工具结果变成
一段长文并挤占上下文——检索的价值在于「指出去哪里看」，不在于把文档搬进对话。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent.tools import ToolRegistry, ToolSource
from knowledge_runtime import ensure_service

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

_MAX_SNIPPET_CHARS = 600
"""单个片段在工具结果里的字符上限。"""

_NO_HITS_TEMPLATE = "没有在工作区已索引的文档中检索到与「{query}」相关的内容。"

_SEARCH_DESCRIPTION = (
    "在工作区已索引的文档里检索相关内容。当问题涉及项目文档、笔记或说明，"
    "而当前上下文里没有这些内容时使用；文档内容可能已被修改，改动后可先调用"
    " index_documents 刷新。返回若干片段，每个片段带来源文件路径与所在小节，"
    "回答时请一并说明来源。"
)

_INDEX_DESCRIPTION = (
    "索引（或刷新）工作区里的文本文档，供后续检索。首次使用知识库、或文档刚被"
    "写入 / 修改之后调用。内容未变的文档会被跳过，因此重复调用代价很低。"
)


class KnowledgeToolError(RuntimeError):
    """知识库工具调用失败（输入不合法或后端不可用）。

    WHY 单独一个异常类型：工具失败要能被工具层如实回传给模型，而「检索词为空」这类
    输入问题与「数据库坏了」这类故障，模型应采取的动作完全不同。
    """


def _truncate(text: str) -> str:
    """按字符上限截断片段，并显式标注截断。"""
    if len(text) <= _MAX_SNIPPET_CHARS:
        return text
    return text[:_MAX_SNIPPET_CHARS] + "…（片段已截断）"


def _build_search_tool(config: AppConfig) -> object:
    """构造 ``search_documents`` 工具。"""
    from langchain_core.tools import tool

    @tool("search_documents", description=_SEARCH_DESCRIPTION)
    async def search_documents(query: str) -> str:
        """在工作区已索引的文档里检索。"""
        service = await ensure_service(config)
        try:
            result = await service.search(query)
        except ValueError as exc:
            # WHY 转成工具错误而不是让它冒泡：``ValueError`` 在这里是「输入不合法」，
            # 模型看到一条明确的原因才能改写检索词重试；原样抛出会被工具层当成
            # 内部故障，模型会放弃而不是重试。
            raise KnowledgeToolError(f"检索词不合法：{exc}") from exc

        hits = result["hits"]
        if not hits:
            return _NO_HITS_TEMPLATE.format(query=result["query"])

        lines = [f"检索到 {len(hits)} 个相关片段："]
        for index, hit in enumerate(hits, start=1):
            heading = f" · {hit['heading']}" if hit["heading"] else ""
            lines.append(f"\n[{index}] {hit['source_path']}{heading}\n{_truncate(hit['body'])}")

        # WHY 把降级写进结果：语义检索失败时关键词结果照常返回，但模型与用户都该知道
        # 「这次没走语义」——否则「某天开始搜得不准」没有任何线索。
        if result["vector_status"] not in ("ok", "disabled"):
            lines.append(f"\n（注意：本次未使用语义检索，状态 {result['vector_status']}）")
        return "\n".join(lines)

    return search_documents


def _build_index_tool(config: AppConfig) -> object:
    """构造 ``index_documents`` 工具。"""
    from langchain_core.tools import tool

    @tool("index_documents", description=_INDEX_DESCRIPTION)
    async def index_documents() -> str:
        """索引（或刷新）工作区文本文档。"""
        service = await ensure_service(config)
        summary = await service.index_workspace()
        return (
            f"已扫描 {summary['scanned']} 个文件：新索引 {summary['indexed']} 个、"
            f"内容未变 {summary['unchanged']} 个、无可索引内容 {summary['empty']} 个、"
            f"跳过 {summary['skipped']} 个。"
        )

    return index_documents


def register_tools(registry: ToolRegistry, config: AppConfig | None = None) -> None:
    """注册知识库工具。

    Args:
        registry: 目标注册器。
        config: 应用配置；本模块必须由 ``register_tools(registry, config)`` 形式加载。

    Raises:
        ValueError: 未提供配置。
    """
    if config is None:
        raise ValueError(
            "knowledge_tools 需要配置对象，请以 register_tools(registry, config) 形式加载"
        )

    registry.register(_build_search_tool(config), source=ToolSource.CUSTOM)
    registry.register(_build_index_tool(config), source=ToolSource.CUSTOM)
    logger.info(
        "知识库工具已注册：search_documents / index_documents（嵌入后端=%s）",
        config.embedding_backend,
    )
