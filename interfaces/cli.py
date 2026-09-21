"""命令行交互适配器。

职责边界：只负责「把事件画出来」和「向用户要决策」，不持有任何 Agent 逻辑。
与 Web 端唯一的区别就是决策来源这里是 stdin。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from application.errors import ThreadBusyError
from application.events import AgentEvent, AgentEventType
from bootstrap.core import build_app_context

if TYPE_CHECKING:
    from config import AppConfig

    from application.run_service import RunService

logger = logging.getLogger(__name__)

_EXIT_COMMANDS = frozenset({"exit", "quit", ":q"})


def _render_todos(items: list[dict[str, Any]]) -> None:
    markers = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}
    print("\n-- 待办 --")
    for item in items:
        marker = markers.get(str(item.get("status", "")), "[ ]")
        print(f"  {marker} {item.get('content', '')}")
    print("")


def render_event(event: AgentEvent) -> None:
    """把单个事件渲染到终端。"""
    payload = event.payload

    if event.event is AgentEventType.TOKEN:
        # WHY 不换行并强制刷缓冲：流式输出必须逐字吐出，否则用户会以为卡死
        print(payload.get("text", ""), end="", flush=True)
    elif event.event is AgentEventType.TOOL_CALL:
        args = payload.get("args", {})
        print(f"\n[调用] {payload.get('name', '')} {_format_args(args)}", flush=True)
    elif event.event is AgentEventType.TOOL_RESULT:
        status = payload.get("status") or ""
        suffix = " (已截断)" if payload.get("truncated") else ""
        print(f"[结果] {payload.get('name', '')} {status}{suffix}", flush=True)
        # 被截断时给出留存位置：命令行里没有文件面板，路径就是唯一的回取入口
        ref = payload.get("full_output_ref")
        if ref:
            print(f"       完整输出：workspace{ref}", flush=True)
    elif event.event is AgentEventType.TODOS:
        _render_todos(list(payload.get("items") or []))
    elif event.event is AgentEventType.STEP:
        pass
    elif event.event is AgentEventType.ERROR:
        print(f"\n[错误] {payload.get('message', '')}", flush=True)
    elif event.event is AgentEventType.USAGE:
        # WHY CLI 也打印用量：命令行是排障与压测的主战场，
        # 「这次跑了多少 token」在这里比在网页上更常被问到。
        print(
            f"\n[用量] 输入 {payload.get('prompt_tokens', 0)} / "
            f"输出 {payload.get('completion_tokens', 0)} / "
            f"合计 {payload.get('total_tokens', 0)} tokens",
            flush=True,
        )
    elif event.event is AgentEventType.DONE:
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
    runs: RunService,
    thread_id: str,
    user_input: str,
    *,
    model_name: str | None = None,
    workspace: str | None = None,
) -> None:
    """执行一轮输入，并在需要时循环处理多次中断。

    Args:
        runs: 运行服务。
        thread_id: 会话 ID。
        user_input: 本轮输入。
        model_name: 模型别名。
        workspace: 本次启动给出的工作空间（``--workspace``）；``None`` 表示不绑定，
            这条 CLI 会话将使用应用为它创建的专属目录。**只在首轮生效**——第一轮之后
            根就锁定了，服务端会拒绝与它不同的取值。
    """
    pending_payload: dict[str, Any] | None = None

    async def consume(events: Any) -> None:
        nonlocal pending_payload
        async for event in events:
            if event.event is AgentEventType.INTERRUPT:
                pending_payload = event.payload
                continue
            render_event(event)

    # WHY 先 await 拿到事件流再消费：``stream`` 是普通协程，参数校验与模型
    # 初始化都在这一步完成，错误能在进入渲染之前抛出，而不是混在事件流里。
    events = await runs.stream(
        thread_id,
        user_input,
        model_name=model_name,
        workspace=workspace,
    )
    await consume(events)

    while pending_payload is not None:
        decision = ask_human(pending_payload)
        pending_payload = None
        # WHY 恢复时同样带上 model_name：中断与恢复是同一次运行的两个半程，
        # 走不同模型会让缓存里多出一个实例，也会让成本与行为出现不可预期偏差。
        # WHY 不再带 workspace：根在第一轮就锁定了，恢复时再传只是重复一个已生效的事实。
        resumed = await runs.resume(thread_id, decision, model_name=model_name)
        await consume(resumed)


async def run_cli(
    config: AppConfig, *, model_name: str | None = None, workspace: str | None = None
) -> int:
    """CLI 主循环。

    Args:
        config: 应用配置。
        model_name: 本次会话使用的模型别名；``None`` 表示用配置里的默认模型。
        workspace: 这条 CLI 会话绑定的工作空间（``--workspace``）；``None`` 表示不绑定，
            它将使用应用为它自动创建的专属目录。CLI 与 Web 的差别就在这里：一个 CLI 进程
            就是一条会话，因此「选择工作空间」发生在启动那一刻，而不是在界面里选。

    Returns:
        进程退出码：0 正常，1 运行期异常，2 参数错误。
    """
    if config is None:
        raise ValueError("config 不能为 None")

    resolved_model = model_name or config.default_model

    # WHY 由上下文管理器托管这些连接：CLI 与 Web 共用同一套生命周期约定，
    # 无论是正常退出还是 Ctrl+C，连接都能被确定关闭而不是依赖 GC。
    # 装配逻辑集中在 bootstrap，两种形态不会出现「一方有审计、另一方没有」
    # 这类难以通过功能测试发现的行为分叉。
    async with build_app_context(config) as context:
        threads = context.threads
        runs = context.runs
        catalog = context.catalog

        # WHY 启动即校验模型别名：等到第一次提问才报「模型不存在」，用户会
        # 以为是网络或密钥问题；而且这里只比对名称，不需要密钥，能快速失败。
        available = {item.name for item in catalog.list_models()}
        if resolved_model not in available:
            print(f"错误：未知模型 {resolved_model!r}，可选：{sorted(available)}")
            return 2

        # 只发号不落库：会话在首轮输入被接受时才登记，直接退出不会留下空会话
        thread_id = threads.new_thread_id()

        print("通用 Agent 已启动，输入 exit 退出。")
        # WHY 打印「这条会话的根」而不是某个配置值：不绑定工作空间时它的专属目录由会话
        # ID 派生（此刻已经拿到 ID），用户需要知道自己的文件到底落在哪里。
        if workspace:
            print(f"工作空间：{Path(workspace).expanduser().resolve()}")
        else:
            print(f"工作空间：未绑定（本会话专属目录：{config.session_dir(thread_id)}）")
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

                await _run_turn(
                runs,
                thread_id,
                user_input,
                model_name=model_name,
                workspace=workspace,
            )
        except ThreadBusyError as exc:
            # WHY 单独提示而不是当成崩溃：CLI 顺序执行本不该并发，出现说明
            # 上一轮的事件流没有被消费完，属于可恢复的状态问题。
            print(f"\n{exc}")
            return 1
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
