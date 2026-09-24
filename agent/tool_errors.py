"""工具异常的统一收敛层：把工具抛出的异常转成模型可读的失败结果。

WHY 必须有这一层（事实陈述，不是偏好）：本项目的工具节点由 langchain 的
``create_agent`` 自行构造，而它在创建 ``ToolNode`` 时**没有**传 ``handle_tool_errors``
（见 ``langchain.agents.factory``）。于是 langgraph 的默认处理器生效，而它只收敛
``ToolInvocationError``（参数不符合 schema），**其余异常一律重新抛出**。异常一旦离开
工具节点，就会一路冒到 ``application.run_service`` 的 ``except Exception``：整轮运行
被记成「运行失败」并向客户端发一个 ERROR 事件，对话就此结束。

WHY 这必须改：工具失败里绝大多数是**可预期的外部结果**——网络超时、上游 4xx/5xx、
重定向超限、响应不是文本、出站安全策略拒绝。这些是「这次调用没成」，不是「这一轮运行
坏了」。模型看到一条 ``status="error"`` 的工具结果，可以换地址、换来源、改参数；运行被
终止时它连尝试的机会都没有。2026-09-23 的实际故障即由此而来：DNS 污染把
``zh.m.wikipedia.org`` 解析到 ``2001::1``，出站校验拒绝后工具抛出异常，整轮对话只给
用户留下一句「目标地址不可访问」。

WHY 收敛点放在中间件而不是逐个工具里 try/except：

1. 一处覆盖全部工具——内置文件工具、``execute``、MCP 工具、``web_tools`` /
   ``knowledge_tools``，以及**将来新增但没人记得加兜底的工具**；``web_fetch`` 裸奔
   就是「靠工具作者自觉必然漏」的证据。
2. 工具自己返回错误文本会把这次调用记成 ``success``（审计按 ``ToolMessage.status``
   判定 outcome），那是观测面的失真；本层产出 ``status="error"``，审计与前端都能
   如实看到失败。
3. 不必改动各工具既有的异常类型与 ``except`` 分支（``WebToolError`` /
   ``KnowledgeToolError`` 等保持原样）。

WHY 放行 ``GraphBubbleUp``：它继承 ``Exception``，但承担的是**控制流**——HITL 审批靠它
把中断冒泡到检查点。被本层吞掉的话，审批会变成一次「工具失败」，而流程继续往下走。
``asyncio.CancelledError`` 继承 ``BaseException``，本层按设计不捕获：停止与断连必须
立刻生效。

WHY 同步钩子也要实现：``langchain.agents.factory`` 会把「只实现了异步钩子」的中间件
一并编进同步链，同步调用路径会撞上基类的 ``NotImplementedError``。

职责边界：本模块只做「收敛 + 留痕」，不判断哪类失败该不该收敛（一律收敛，穿透清单只有
控制流异常）；措辞属于本层，因为工具结果同时是模型与用户能看到的那一份。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command

from agent.run_context import AgentRunContext
from text_utils import truncate_with_notice

logger = logging.getLogger(__name__)

_MAX_ERROR_CHARS = 2000
"""单条错误详情写入工具结果的字符上限。

WHY 需要上限：异常正文的长度不受控（上游把整个响应体塞进异常、包装异常层层拼接），
而工具结果会整体进入上下文——一次报错挤掉对话历史不划算。
"""

_TRUNCATION_NOTICE = "\n\n（错误详情过长，已截断）"

_ERROR_TEMPLATE = (
    "工具 {name} 执行失败，已按错误结果回传（{kind}）：{detail}\n"
    "请调整参数或改用其它途径完成目标；不要原样重复同一次调用。"
)
"""回传模型的失败文案。

WHY 写明「不要原样重复」：工具失败多数源于参数或外部状态，而模型最常见的反应是把同一次
调用再发一遍。「最多能跑几轮」已由 ``ModelCallLimitMiddleware`` 兜底，这里只需把「不该
重试」这件事说清楚，无需再自建计数逻辑。
"""


def _render_error(name: str, exc: Exception) -> str:
    """把异常渲染成给模型看的失败文本。

    Args:
        name: 工具名（模型据此知道是哪次调用失败了）。
        exc: 工具抛出的异常。

    Returns:
        形如「工具 X 执行失败，已按错误结果回传（异常类型）：详情」的文本。
    """
    detail = str(exc).strip() or "（异常没有携带详情）"
    return _ERROR_TEMPLATE.format(
        name=name,
        kind=type(exc).__name__,
        detail=truncate_with_notice(detail, _MAX_ERROR_CHARS, _TRUNCATION_NOTICE),
    )


def _converge(request: ToolCallRequest, exc: Exception) -> ToolMessage:
    """记录异常并构造 ``status="error"`` 的工具结果。

    WHY 日志与构造写在一处：这一层是异常被**消费掉**的唯一位置——若不在此留痕，一次
    工具失败在日志里将完全不可见（工具节点不会重抛，审计只记 outcome，没有堆栈）。
    用 ``exc_info=exc`` 而不是 ``logger.exception``，是为了不依赖「调用点仍在 except
    块内」这一隐含前提，本函数将来被别处复用也不会丢堆栈。

    Args:
        request: 触发失败的工具调用请求。
        exc: 工具抛出的异常。

    Returns:
        可直接交回工具节点的失败结果。
    """
    call = request.tool_call
    # WHY 用 ``get`` 而不是下标：本函数本身处在错误路径上，不该因为一个缺失的 id
    # 再抛一次 KeyError（那会把「工具失败」升级成「中间件失败」）。
    name = str(call.get("name") or "unknown")
    logger.error(
        "工具调用失败，已按错误结果回传模型：tool=%s id=%s",
        name,
        call.get("id"),
        exc_info=exc,
    )
    return ToolMessage(
        content=_render_error(name, exc),
        name=name,
        tool_call_id=str(call.get("id") or ""),
        status="error",
    )


class ToolErrorConvergenceMiddleware(AgentMiddleware[Any, AgentRunContext, Any]):
    """把工具异常收敛成 ``status="error"`` 的工具结果（动机与边界见模块 docstring）。

    WHY 无状态、无可配置项：策略只有一条——除控制流异常外一律收敛。给它加开关或白名单
    会让「某类失败终结整轮运行」重新变成可能，而这类配置漏配的症状极难排查。
    """

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """同步钩子；语义与 :meth:`awrap_tool_call` 完全一致。

        Raises:
            GraphBubbleUp: 原样放行，图中断由上层处理。
        """
        try:
            return handler(request)
        except GraphBubbleUp:
            logger.debug(
                "工具调用触发图中断，原样放行：tool=%s", request.tool_call.get("name")
            )
            raise
        except Exception as exc:  # noqa: BLE001 —— 收敛是唯一目的，见模块 docstring
            return _converge(request, exc)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """异步钩子：工具抛异常时返回失败结果，其余情况原样透传。

        Raises:
            GraphBubbleUp: 原样放行，图中断由上层处理。
        """
        try:
            return await handler(request)
        except GraphBubbleUp:
            logger.debug(
                "工具调用触发图中断，原样放行：tool=%s", request.tool_call.get("name")
            )
            raise
        except Exception as exc:  # noqa: BLE001 —— 收敛是唯一目的，见模块 docstring
            return _converge(request, exc)


__all__ = ["ToolErrorConvergenceMiddleware"]
