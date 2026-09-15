"""命令行交互适配器。

职责边界：只负责「把事件画出来」和「向用户要决策」，不持有任何 Agent 逻辑。
与 Web 端唯一的区别就是决策来源这里是 stdin。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from application.agent_service import AgentService
from application.events import SSEEvent, SSEEventType
from runtime.checkpointer import checkpointer_context
from runtime.thread_store import open_thread_store

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

_EXIT_COMMANDS = frozenset({"exit", "quit", ":q"})


def _render_todos(items: list[dict[str, Any]]) -> None:
    markers = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}
    print("\n-- 待办 --")
    for item in items:
        marker = markers.get(str(item.get("status", "")), "[ ]")
        print(f"  {marker} {item.get('content', '')}")
    print("")


def render_event(event: SSEEvent) -> None:
    """把单个事件渲染到终端。"""
    payload = event.payload

    if event.event is SSEEventType.TOKEN:
        # WHY 不换行并强制刷缓冲：流式输出必须逐字吐出，否则用户会以为卡死
        print(payload.get("text", ""), end="", flush=True)
    elif event.event is SSEEventType.TOOL_CALL:
        args = payload.get("args", {})
        print(f"\n[调用] {payload.get('name', '')} {_format_args(args)}", flush=True)
    elif event.event is SSEEventType.TOOL_RESULT:
        status = payload.get("status") or ""
        suffix = " (已截断)" if payload.get("truncated") else ""
        print(f"[结果] {payload.get('name', '')} {status}{suffix}", flush=True)
    elif event.event is SSEEventType.TODOS:
        _render_todos(list(payload.get("items") or []))
    elif event.event is SSEEventType.STEP:
        pass
    elif event.event is SSEEventType.ERROR:
        print(f"\n[错误] {payload.get('message', '')}", flush=True)
    elif event.event is SSEEventType.DONE:
        print("", flush=True)


def _format_args(args: Any) -> str:
    """把工具参数压成单行摘要，避免长参数刷屏。"""
    if isinstance(args, dict) and "__raw__" in args:
        return str(args["__raw__"])[:80]
    if isinstance(args, dict):
        parts = []
        for key, value in list(args.items())[:4]:
            parts.append(f"{key}={str(value)[:40]}")
        return ", ".join(parts)
    return str(args)[:80]


def _allowed_decisions(action_name: str, review_configs: list[dict[str, Any]]) -> list[str]:
    """取出某次调用允许的审批类型。"""
    for config in review_configs:
        if config.get("action_name") == action_name:
            decisions = config.get("allowed_decisions") or ["approve", "reject"]
            return list(decisions)
    return ["approve", "reject"]


def ask_human(payload: dict[str, Any]) -> dict[str, Any]:
    """向用户索取审批决策。

    Returns:
        符合 ``HITLResponse.decisions`` 要求的载荷。
    """
    action_requests = list(payload.get("action_requests") or [])
    review_configs = list(payload.get("review_configs") or [])
    decisions: list[dict[str, Any]] = []

    for request in action_requests:
        name = request.get("name", "")
        allowed = _allowed_decisions(name, review_configs)

        print("\n== 需要审批 ==")
        print(f"工具：{name}")
        print(f"说明：{request.get('description', '')}")
        print(f"参数：{_format_args(request.get('args', {}))}")

        options = {"approve": "a", "reject": "r", "edit": "e", "respond": "s"}
        available = [f"{options[key]}={key}" for key in allowed if key in options]
        print(f"可选：{', '.join(available)}")

        while True:
            raw = input("你的决定 > ").strip().lower()
            chosen = next((key for key, short in options.items() if short == raw), None)
            if chosen is None and raw in allowed:
                chosen = raw
            if chosen not in allowed:
                print(f"无效输入，请从 {sorted(allowed)} 中选择")
                continue

            decision: dict[str, Any] = {"type": chosen}
            if chosen in ("reject", "respond"):
                decision["message"] = input("补充说明 > ").strip()
            elif chosen == "edit":
                raw_json = input("新的参数(JSON) > ").strip()
                try:
                    import json

                    decision["edited_action"] = {
                        "name": name,
                        "args": json.loads(raw_json),
                    }
                except json.JSONDecodeError as exc:
                    print(f"JSON 解析失败({exc})，请重新输入")
                    continue
            decisions.append(decision)
            break

    return {"decisions": decisions}


async def _run_turn(
    service: AgentService,
    thread_id: str,
    user_input: str,
    model_name: str | None = None,
) -> None:
    """执行一轮输入，并在需要时循环处理多次中断。"""
    pending_payload: dict[str, Any] | None = None

    async def consume(events: Any) -> None:
        nonlocal pending_payload
        async for event in events:
            if event.event is SSEEventType.INTERRUPT:
                pending_payload = event.payload
                continue
            render_event(event)

    await consume(service.stream(thread_id, user_input, model_name=model_name))

    while pending_payload is not None:
        decision = ask_human(pending_payload)
        pending_payload = None
        # WHY 恢复时同样带上 model_name：中断与恢复是同一次运行的两个半程，
        # 走不同模型会让缓存里多出一个实例，也会让成本与行为出现不可预期偏差。
        await consume(service.resume(thread_id, decision, model_name=model_name))


async def run_cli(config: AppConfig, *, model_name: str | None = None) -> int:
    """CLI 主循环。

    Args:
        config: 应用配置。
        model_name: 本次会话使用的模型别名；``None`` 表示用配置里的默认模型。

    Returns:
        进程退出码：0 正常，1 运行期异常。
    """
    if config is None:
        raise ValueError("config 不能为 None")

    resolved_model = model_name or config.default_model

    # WHY 由上下文管理器托管这两类连接：CLI 与 Web 共用同一套生命周期约定，
    # 无论是正常退出还是 Ctrl+C，连接都能被确定关闭而不是依赖 GC。
    # 元数据存储同样要开：否则 CLI 创建的会话不会出现在 Web 的会话清单里。
    async with (
        checkpointer_context(config.db_path) as checkpointer,
        open_thread_store(config.db_path) as thread_store,
    ):
        service = AgentService(
            config,
            checkpointer=checkpointer,
            thread_store=thread_store,
        )

        # WHY 启动即校验模型别名：等到第一次提问才报「模型不存在」，用户会
        # 以为是网络或密钥问题；而且这里只比对名称，不需要密钥，能快速失败。
        available = {item["name"] for item in service.models()}
        if resolved_model not in available:
            print(f"错误：未知模型 {resolved_model!r}，可选：{sorted(available)}")
            return 2

        # 只发号不落库：会话在首轮输入被接受时才登记，直接退出不会留下空会话
        thread_id = service.new_thread()

        print("通用 Agent 已启动，输入 exit 退出。")
        print(f"工作区：{config.workspace}")
        print(f"执行档位：{config.execution_mode.value}")
        print(f"当前模型：{resolved_model}")
        print(f"会话 ID：{thread_id}")

        try:
            while True:
                try:
                    user_input = input("\n> ").strip()
                except EOFError:
                    # WHY 捕获 EOF：管道输入场景（echo "hi" | python main.py cli）
                    # 会在读完后抛 EOFError，这是正常结束而非错误。
                    print("")
                    break

                if not user_input:
                    continue
                if user_input.lower() in _EXIT_COMMANDS:
                    break

                await _run_turn(service, thread_id, user_input, model_name)
        except KeyboardInterrupt:
            print("\n已中断")
            return 0
        except Exception:
            logger.exception("CLI 运行失败")
            return 1

    return 0


def main_sync(config: AppConfig, *, model_name: str | None = None) -> int:
    """同步入口，供 ``main.py`` 调用。"""
    return asyncio.run(run_cli(config, model_name=model_name))
