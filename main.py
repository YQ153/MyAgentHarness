"""应用入口：分发 CLI 与 Web 两种运行形态。

两种形态共享同一套内核（装配层 + 应用服务），差异只在适配器。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from application.audit_context import current_request_context
from config import AppConfig


class _JsonFormatter(logging.Formatter):
    """把日志记录序列化成一行 JSON。

    WHY 自己写而不是引第三方：需要的字段就六个，而格式化器是唯一必须知道
    「这条日志属于哪次请求」的地方——自己写才能让 trace_id 的来源只有一个。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id = record.trace_id if hasattr(record, "trace_id") else ""
        if trace_id:
            payload["trace_id"] = trace_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _TraceFilter(logging.Filter):
    """把当前请求的 trace_id 注入每条日志记录。

    WHY 用 Filter 而不是让各处手动传：日志调用点遍布全仓，逐个补参数必然漏；
    而 trace_id 本就存在 contextvar 里，读取是一次无副作用的查找。

    WHY 只加字段不删记录：没绑定上下文时（CLI、后台任务）注入空串，日志照常输出。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_request_context().trace_id
        return True


def _setup_logging(level: str, log_format: str = "text") -> None:
    """配置全局日志。

    WHY 必须显式设置 handlers：不设置时可能继承到第三方库的配置，导致
    ``uvicorn`` 重复接管日志，出现每条消息打印两遍的情况。

    WHY ``text`` 也带 trace_id：两种格式都要能按链路串起一次请求，否则「切到
    JSON 才能排查」会让本机开发被迫先改配置。
    """
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_TraceFilter())

    if str(log_format).lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                # trace_id 为空时只留一个短横线占位，不打印空白字段
                fmt="%(asctime)s %(levelname)-7s [%(trace_id)s] %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    logging.basicConfig(level=numeric_level, handlers=[handler], force=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="通用 Agent：基于 deepagents 的任务助手",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # WHY 只有 cli 有 --workspace：一个 CLI 进程就是一条会话，「选择工作空间」发生在启动
    # 那一刻（与 Web 形态在界面上选是同一件事的两种形态）。而 Web 进程会承载很多条会话，
    # 它的工作空间由每条会话各自决定——在启动参数上再放一个默认值，只会让「不选」与
    # 「选了配置里那个」变成同一件事，而它们在本模型下必须落到不同的根上。
    cli = sub.add_parser("cli", help="命令行交互式运行")
    cli.add_argument("--model", default=None, help="模型别名，默认取配置中的 default_model")
    cli.add_argument(
        "--workspace",
        default=None,
        help=(
            "本次 CLI 会话的工作空间（Agent 的文件根）。必须是已存在的目录；"
            "不传表示不绑定——本会话将使用应用为它自动创建的专属目录"
        ),
    )

    web = sub.add_parser("web", help="启动 Web 服务")
    web.add_argument("--host", default=None, help="监听地址，默认取配置")
    web.add_argument("--port", type=int, default=None, help="监听端口，默认取配置")

    parser.add_argument("--log-level", default=None, help="日志级别：DEBUG/INFO/WARNING/ERROR")
    return parser


def _run_cli(config: AppConfig, model_name: str | None, workspace: str | None) -> int:
    from interfaces.cli import run_cli

    # CLI 不监听端口，但「HOST 非回环 + 未启用认证」说明的是部署形态不安全，
    # 而同一份 .env 通常也用于 Web 形态；在跑 CLI 时就提示，比等暴露之后再
    # 从别处发现更早。
    config.warn_if_unauthenticated_exposure()
    return asyncio.run(run_cli(config, model_name=model_name, workspace=workspace))


def _run_web(config: AppConfig, host: str | None, port: int | None) -> int:
    import uvicorn

    from interfaces.web.app import create_app

    # 命令行 --host 优先于配置，因此自检必须盯住「最终真正绑定的地址」：
    # 若只检查 config.host，`main.py web --host 0.0.0.0` 恰好绕过了这条检查。
    bind_host = host or config.host
    config.warn_if_unauthenticated_exposure(bind_host)

    app = create_app(config)
    try:
        uvicorn.run(
            app,
            host=bind_host,
            port=port or config.port,
            log_level=config.log_level.lower(),
        )
    except KeyboardInterrupt:
        # 正常停止，不应被当成崩溃
        print("\n服务已停止", file=sys.stderr)
    except Exception:
        logging.getLogger(__name__).exception("Web 服务异常退出")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """程序入口，返回进程退出码。"""
    args: Any = _build_parser().parse_args(argv)

    try:
        config = AppConfig.load()
    except Exception:
        # WHY 兜住配置异常：这是唯一无法继续的阶段，必须给出清晰提示而不是
        # 让 pydantic 的原始校验栈直接糊到用户脸上。
        logging.getLogger(__name__).exception("配置加载失败")
        print(
            "配置加载失败，请检查 .env 文件与环境变量（详见 .env.example）",
            file=sys.stderr,
        )
        return 2

    _setup_logging(args.log_level or config.log_level, config.log_format)

    if args.command == "cli":
        return _run_cli(config, args.model, args.workspace)
    return _run_web(config, args.host, args.port)


if __name__ == "__main__":
    sys.exit(main())
