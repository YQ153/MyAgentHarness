"""命令行交互适配器。

职责边界：只负责「把事件画出来」和「向用户要决策」，不持有任何 Agent 逻辑。
与 Web 端唯一的区别就是决策来源这里是 stdin。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from application.api_key_auth import record_api_key_auth, validate_api_key
from application.errors import ThreadBusyError
from application.events import AgentEvent, AgentEventType
from application.ports import APIKeyRepository, AuditSink
from application.principal import Principal
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


async def _validate_api_key_for_cli(
    config: AppConfig,
    api_key: str,
    *,
    api_key_store: APIKeyRepository | None = None,
    audit_store: AuditSink | None = None,
) -> Principal:
    """校验单个 API Key，返回 Principal；校验失败直接抛异常。

    WHY 保留这层薄封装：CLI 的失败语义是"抛异常让 ``run_cli`` 打印提示并以 2 退出"，
    而共用实现用 ``principal=None`` 表达失败（Web 侧要的是"按未认证处理"）。
    两者的差异只在翻译方式上，校验本身已经共用。

    WHY 审计写在这里：失败路径上只有本函数拿得到结果对象——上层只看到一个异常，
    写不出失败原因，而"为什么失败"正是审计里最有用的一列
    （存储没装配 vs 凭据无效，处置方式完全不同）。
    """
    result = await validate_api_key(
        api_key, dev_key=config.auth_api_key_dev, store=api_key_store
    )
    # WHY entry="cli" 且不传 ip / user_agent：CLI 根本没有这两个字段。不打入口标记的话，
    # 这条审计在面板上与一次 HTTP 请求长得完全一样——看不出"有人在本机命令行用了这把 Key"。
    await record_api_key_auth(audit_store, result, entry="cli")
    if result.principal is None:
        raise ValueError("HARNESS_API_KEY 无效")
    return result.principal


async def _build_cli_principal(
    config: AppConfig,
    *,
    api_key_store: APIKeyRepository | None = None,
    audit_store: AuditSink | None = None,
) -> Principal | None:
    """根据配置构造认证主体。

    - disabled：返回 ``None``，走匿名兼容路径。
    - apikey：必须提供 API Key（``HARNESS_API_KEY``，写在 ``.env`` 或同名环境变量里），
      校验后返回对应主体。
    """
    if config.auth_mode == "disabled":
        return None

    # WHY 从配置对象读而不是直接读 ``os.environ``：``.env`` 里的值只进 ``AppConfig``、
    # 不进进程环境变量（本项目没有 load_dotenv），读 os.environ 会让「照着
    # .env.example 配好了却仍提示没配」成为必然。真实环境变量由 pydantic-settings
    # 以更高优先级读进同一个字段，两种来源都汇到这一处。
    api_key = config.harness_api_key.strip()
    if not api_key:
        raise ValueError(
            "auth_mode=apikey 时，请在 .env 里设置 HARNESS_API_KEY（或设置同名环境变量）后启动 CLI"
        )
    return await _validate_api_key_for_cli(
        config,
        api_key,
        api_key_store=api_key_store,
        audit_store=audit_store,
    )


async def _run_turn(
    runs: RunService,
    thread_id: str,
    user_input: str,
    *,
    principal: Principal | None = None,
    model_name: str | None = None,
) -> None:
    """执行一轮输入，并在需要时循环处理多次中断。"""
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
    events = await runs.stream(thread_id, user_input, principal=principal, model_name=model_name)
    await consume(events)

    while pending_payload is not None:
        decision = ask_human(pending_payload)
        pending_payload = None
        # WHY 恢复时同样带上 model_name：中断与恢复是同一次运行的两个半程，
        # 走不同模型会让缓存里多出一个实例，也会让成本与行为出现不可预期偏差。
        resumed = await runs.resume(thread_id, decision, principal=principal, model_name=model_name)
        await consume(resumed)


async def run_cli(config: AppConfig, *, model_name: str | None = None) -> int:
    """CLI 主循环。

    Args:
        config: 应用配置。
        model_name: 本次会话使用的模型别名；``None`` 表示用配置里的默认模型。

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
        try:
            principal = await _build_cli_principal(
                config,
                api_key_store=context.api_key_store,
                # WHY 传的正是 bootstrap 装配的那一份：与 Web 侧同一个对象、同一张表，
                # 于是两个入口的认证审计可以并排看——这正是本次收敛要的结果。
                audit_store=context.audit_store,
            )
        except ValueError as exc:
            # WHY 在这里收住而不是让它抛到 main()：密钥缺失或无效属于用户当场就能
            # 修正的配置问题，抛出去会让终端刷出一份与原因无关的堆栈（还要叠加装配
            # 退出路径上的日志），真正的一句提示被埋在中间。退出码 2 与「参数错误」
            # 同义，脚本据此可以区分「配置没给」与「跑起来之后失败」（后者是 1）。
            print(f"错误：{exc}")
            print(
                "提示：在 .env 里设置 HARNESS_API_KEY（本机开发可直接填 AUTH_API_KEY_DEV 的值）；"
                "生产环境应改用管理面板创建的 Key。"
            )
            return 2
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

                await _run_turn(runs, thread_id, user_input, principal=principal, model_name=model_name)
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
