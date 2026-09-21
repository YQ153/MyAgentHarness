"""知识库自定义工具：让 Agent 自主检索并索引工作区文档。

启用方式：``CUSTOM_TOOL_MODULES=knowledge_tools``

WHY 做成自定义工具而不是内置工具：知识库是可选能力（要装 ``sqlite-vec``、可能要配
嵌入后端），塞进内置工具集会让没启用它的部署也背上一段工具描述与失败面；T8 的扩展点
正是为这类「按配置启用」的能力准备的。

WHY 工具体内按需取服务而不是注册时注入：扩展点只把 ``config`` 交给模块，而知识库
持有的是一条 SQLite 连接。``knowledge_runtime`` 负责按工作区各持一份，这里只取用。

WHY 服务要按**本轮运行的工作区**取，而不是取启动时那一个：知识库按工作区隔离，每条
会话的索引只包含它自己工作区里的文档。取错库的表现是「检索到的文档不是这个项目的」，
而它看起来像检索不准，不像配错了库。工作区由 ``AgentRunContext`` 经 ``ToolRuntime``
传进来（与长期记忆的按主体隔离共用同一条通道）。

WHY 检索结果要截断：一次返回若干个片段，每个都可能上千字；原样返回会把工具结果变成
一段长文并挤占上下文——检索的价值在于「指出去哪里看」，不在于把文档搬进对话。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

# WHY 运行期导入而不是放进 TYPE_CHECKING：``@tool`` 会调 ``get_type_hints`` 解析注解，
# 而注解在 ``from __future__ import annotations`` 下是字符串——名字必须真的存在于模块
# 命名空间里，否则构造工具时就抛 NameError（失败点离原因很远，看起来像「知识库工具
# 注册不了」）。
from langchain.tools import ToolRuntime

from agent.run_context import workspace_of
from agent.tools import ToolRegistry, ToolSource
from knowledge_runtime import ensure_service
from text_utils import truncate_with_notice

if TYPE_CHECKING:
    from application.knowledge_service import KnowledgeService
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
    """按字符上限截断片段，并显式标注截断。

    WHY 把判断交给 ``text_utils.truncate_with_notice``：截断与否、切到哪里是
    与网页正文截断共用的同一个决策，分开写迟早出现「这里标了、那里没标」。
    留在本模块的只有措辞——这段文本直接进对话，读者是人，所以用中文短句，
    不用英文与字符数。
    """
    return truncate_with_notice(text, _MAX_SNIPPET_CHARS, "…（片段已截断）")


async def _service_for(config: AppConfig, runtime: ToolRuntime) -> KnowledgeService:
    """按本轮运行的文件根取知识库服务。

    WHY **没有**回落分支：库按根隔离（同一个 ``/README.md`` 在两个项目里是同一个键），
    因此「拿不准用哪个根」时唯一安全的做法是报错而不是猜一个。运行上下文没带根，说明
    调用方绕过了图（图在构造时就把根烧进了 backend 与工具上下文）——那时查到的索引
    很可能属于另一个项目，而症状只是「检索结果对不上」，不会报任何错。

    Raises:
        KnowledgeToolError: 运行上下文里没有文件根。

    Args:
        config: 应用配置。
        runtime: 工具运行时；从中取 ``AgentRunContext.workspace``（本轮的文件根）。
    """
    workspace = workspace_of(runtime)
    if not workspace:
        raise KnowledgeToolError(
            "本轮运行没有文件根，无法确定要检索哪一个知识库；"
            "请通过对话发起检索（图会在构造时把根写进运行上下文）"
        )
    return await ensure_service(config, Path(workspace))


def _build_search_tool(config: AppConfig) -> object:
    """构造 ``search_documents`` 工具。"""
    from langchain_core.tools import tool

    @tool("search_documents", description=_SEARCH_DESCRIPTION)
    async def search_documents(query: str, runtime: ToolRuntime) -> str:
        """在工作区已索引的文档里检索。"""
        service = await _service_for(config, runtime)
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
    async def index_documents(runtime: ToolRuntime) -> str:
        """索引（或刷新）工作区文本文档。"""
        service = await _service_for(config, runtime)
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
