"""应用入口：分发 CLI 与 Web 两种运行形态。

两种形态共享同一套内核（装配层 + 应用服务），差异只在适配器。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from config import AppConfig


def _setup_logging(level: str) -> None:
    """配置全局日志。

    WHY 必须显式设置 handlers：不设置时可能继承到第三方库的配置，导致
    ``uvicorn`` 重复接管日志，出现每条消息打印两遍的情况。
    """
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="通用 Agent：基于 deepagents 的任务助手",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cli = sub.add_parser("cli", help="命令行交互式运行")
    cli.add_argument("--model", default=None, help="模型别名，默认取配置中的 default_model")

    web = sub.add_parser("web", help="启动 Web 服务")
    web.add_argument("--host", default=None, help="监听地址，默认取配置")
    web.add_argument("--port", type=int, default=None, help="监听端口，默认取配置")

    parser.add_argument("--log-level", default=None, help="日志级别：DEBUG/INFO/WARNING/ERROR")
    return parser


def _run_cli(config: AppConfig, model_name: str | None) -> int:
    from interfaces.cli import run_cli

    # CLI 不监听端口，但「HOST 非回环 + 未启用认证」说明的是部署形态不安全，
    # 而同一份 .env 通常也用于 Web 形态；在跑 CLI 时就提示，比等暴露之后再
    # 从别处发现更早。
    config.warn_if_unauthenticated_exposure()
    return asyncio.run(run_cli(config, model_name=model_name))


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

    _setup_logging(args.log_level or config.log_level)

    if args.command == "cli":
        return _run_cli(config, args.model)
    return _run_web(config, args.host, args.port)


if __name__ == "__main__":
    sys.exit(main())
