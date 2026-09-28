"""知识库真机冒烟：从装配到「让 Agent 用的工具检索到」走完整条链路。

与单元测试的分工：那边用替身嵌入与临时目录验编排，这里用**真实的嵌入后端**（默认
``subprocess``，即独立 venv 里的真模型）与真实的 ``sqlite-vec`` 扩展验一遍装配——
单元测试覆盖不到的恰恰是这些「装不上 / 连不通 / 没接上」的失败。

WHY 工具要经 ``build_tool_bundle`` 而不是直接调 ``register_tools``：后者绕过了真正
的启用路径。``CUSTOM_TOOL_MODULES=knowledge_tools`` 这条路径上有自己的环节——模块按
点分路径导入、钩子签名判定、工具名冲突检查——直接调用会把它们全部跳过，于是「文档说
这么启用，实际启用不了」这类问题不会被这条冒烟发现。

WHY 全程只在临时目录里跑：它会索引并落库。指向真实工作区会往用户的资料里塞测试文件，
也会污染真实的知识库。

用法::

    python scripts/smoke_knowledge.py                    # subprocess 嵌入 + 真模型
    python scripts/smoke_knowledge.py --backend none     # 只用关键词（不需要模型环境）

退出码：``0`` 通过 / ``2`` 有跳过（嵌入环境未就绪）/ ``1`` 失败。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# WHY 强制 UTF-8：语料与断言都是中文，Windows 控制台默认 GBK 会让 print 抛
# UnicodeEncodeError，把一次成功的验收变成假失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent.run_context import AgentRunContext  # noqa: E402
from agent.tooling import build_tool_bundle  # noqa: E402
from bootstrap.core import build_app_context  # noqa: E402
from config import AppConfig  # noqa: E402
from knowledge_runtime import (  # noqa: E402
    close_service,
    ensure_service,
    knowledge_db_path,
    peek_service,
)
from langchain.tools import ToolRuntime  # noqa: E402
from llm.embed_process import default_embed_python  # noqa: E402

_DOC = """# 登录服务运维手册

## 超时排查

登录接口偶发超时，p99 达到 3 秒。先看连接池的 max_overflow 是否被压满。

## 幂等

幂等键由客户端生成，服务端只做校验，不负责去重。
"""

_QUERY = "登录变慢怎么排查"
"""与文档**没有任何字面重合**的查询。

WHY 特意选它：若检索只是关键词碰巧命中，这条查询不会命中任何片段；它能命中
「超时排查」小节，才说明语义检索真的在工作。
"""


def _session_root(config: AppConfig, name: str) -> pathlib.Path:
    """取一个冒烟用的会话根。

    WHY 不再读某个「启动默认工作区」：新模型下每个会话的根由它自己决定——用户选的
    工作空间，或应用为它建的专属目录。本脚本是一段「手工会话」，因此显式取一个专属
    目录（与 ``scripts/smoke_sandbox.py`` 同一口径）。

    Args:
        config: 应用配置。
        name: 会话标识，用它派生专属目录名。

    Returns:
        会话根路径（目录由调用方按需创建）。
    """
    return config.session_dir(name)


async def _run(config: AppConfig) -> int:
    """装配 → 索引 → 检索，返回退出码。"""
    root = _session_root(config, "smoke-knowledge")
    root.mkdir(parents=True, exist_ok=True)
    (root / "login.md").write_text(_DOC, encoding="utf-8")

    print("[1/5] 经 build_app_context 装配（与 CLI / Web 启动同一条路径）")
    async with build_app_context(config) as context:
        # WHY 按会话根装配而不是取 ``context`` 上的字段：``AppContext`` 是**应用级**
        # 依赖集合，故意没有 ``knowledge``（它按文件根各有一份，启动时一个根都不存在）。
        # 本脚本是一段手工会话，因此显式用自己的根装配——这也正是工具侧与接口的取法。
        knowledge = await ensure_service(config, workspace=root)
        capabilities = knowledge.capabilities()
        print(f"      知识库就位：{knowledge_db_path(config, root)}")
        print(
            f"      向量检索={capabilities['vector_enabled']} "
            f"嵌入={capabilities['embedding_backend']}"
        )
        if peek_service(root) is not knowledge:
            print("[FAIL] 工具侧句柄与本次装配的不是同一个实例")
            return 1

        print("[2/5] 索引工作区")
        summary = await knowledge.index_workspace()
        print(
            f"      扫描 {summary['scanned']}，新索引 {summary['indexed']}，"
            f"未变化 {summary['unchanged']}，跳过 {summary['skipped']}"
        )
        # WHY 断言「新索引 + 未变化」而不是只看新索引：自动同步在装配时就跑过首轮，
        # 很可能已经把这份文档索引好了，于是这里的手动索引会如实返回 ``unchanged``。
        # 两种结果都说明索引已到位，只有两者都为 0 才是真没索引上。
        if summary["indexed"] + summary["unchanged"] != 1:
            print(
                f"[FAIL] 期望库里有 1 个文件，实际新索引 {summary['indexed']}、"
                f"未变化 {summary['unchanged']}"
            )
            return 1

        print(f"[3/5] 服务层检索：{_QUERY!r}")
        direct = await knowledge.search(_QUERY)
        if not direct["hits"]:
            print("[FAIL] 服务层没有检索到任何片段")
            return 1
        for hit in direct["hits"][:2]:
            heading = f" · {hit['heading']}" if hit["heading"] else ""
            print(f"      [{hit['mode']}] {hit['source_path']}{heading}")
        print(f"      vector_status={direct['vector_status']}")

        print("[4/5] 经真实启用路径装配工具（CUSTOM_TOOL_MODULES=knowledge_tools）")
        bundle = await build_tool_bundle(config)
        names = sorted(item.name for item in bundle.tools)
        print(f"      装配出的工具：{names}")
        missing = {"search_documents", "index_documents"} - set(names)
        if missing:
            print(f"[FAIL] 启用路径未注册出这些工具：{sorted(missing)}")
            return 1

        search_tool = next(item for item in bundle.tools if item.name == "search_documents")
        # WHY 手工构造 ``ToolRuntime``：工具声明了 ``runtime: ToolRuntime`` 参数，平时由图
        # 在调用时注入；冒烟直接 ``ainvoke`` 不经过图，必须自己带上。带上它才有意义——
        # 工具正是靠 ``context.workspace`` 决定查哪一个工作区的库，因此这一步同时验了
        # 「工具取的是本轮工作区的索引」，而不是恰好拿到启动时的那一个。
        runtime = ToolRuntime(
            state={},
            context=AgentRunContext(workspace=str(root)),
            config={},
            stream_writer=lambda *_args, **_kwargs: None,
            tool_call_id="smoke-knowledge",
            # WHY 显式给 ``store=None``：它是必填位置参数（知识库工具用不到 Store，
            # 但不给就构造不出来）。
            store=None,
        )
        rendered = await search_tool.ainvoke({"query": _QUERY, "runtime": runtime})
        if "/login.md" not in rendered:
            print("[FAIL] 工具结果里没有来源文件：")
            print(rendered[:400])
            return 1
        print("      [OK  ] 工具结果带来源文件与所在小节")

        if capabilities["vector_enabled"] and direct["vector_status"] != "ok":
            print(f"[FAIL] 已启用向量检索但本次未走语义路径：{direct['vector_status']}")
            return 1

    print("[5/5] 退出装配上下文")
    if peek_service() is not None:
        print("[FAIL] 上下文退出后知识库句柄未清理")
        return 1
    print("      [OK  ] 句柄已清理")

    if not capabilities["vector_enabled"]:
        print("\n[SKIP] 未启用嵌入后端：本次只验证关键词链路")
        return 2

    print("\n[PASS] 知识库真机冒烟通过（装配 → 索引 → 语义检索 → 启用路径上的 Agent 工具）")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="知识库真机冒烟")
    parser.add_argument(
        "--backend",
        default="subprocess",
        choices=["none", "openai-compat", "subprocess"],
        help="覆盖 EMBEDDING_BACKEND（默认 subprocess）",
    )
    parser.add_argument(
        "--python",
        default="",
        help="subprocess 档位的解释器；留空用 <项目>/.data/embed-venv",
    )
    args = parser.parse_args(argv)

    python = args.python or str(default_embed_python(ROOT / ".data"))
    if args.backend == "subprocess" and not pathlib.Path(python).exists():
        print(f"[SKIP] 嵌入环境不存在：{python}")
        print("       先执行：python scripts/setup_embed_venv.py")
        return 2

    with tempfile.TemporaryDirectory(prefix="mah-knowledge-") as workdir:
        root = pathlib.Path(workdir)
        # WHY 不建任何「工作区」目录：配置里不再有这一项——会话的根由它自己决定。本脚本
        # 造的是一个手工会话，因此下面显式取一个会话根来放文档。
        config = AppConfig(
            _env_file=root / "no-such.env",
            memory_file=root / "AGENTS.md",
            db_path=root / "data" / "agent.db",
            skill_dirs=[root / "skills"],
            embedding_backend=args.backend,
            embedding_python=python,
            # WHY 显式把权重缓存指到项目自己的 ``.data/embed-cache``：本脚本的数据目录是
            # 临时目录，若按「缓存随数据目录」的默认推导，每次跑都会得到一个空缓存、
            # 于是每次都要重新下载 90 MB（离线环境下直接失败）。冒烟要验的是**链路**，
            # 不是下载，因此复用已经下好的那一份。
            embedding_cache_dir=str(ROOT / ".data" / "embed-cache"),
            # WHY 只用真实启用路径来注册工具：直接调 register_tools 会绕开
            # 「模块导入 / 钩子签名判定 / 冲突检查」这几个环节，而问题往往就出在那里。
            custom_tool_modules=["knowledge_tools"],
        )
        print(
            f"配置：backend={config.embedding_backend} model={config.embedding_model} "
            f"dims={config.embedding_dims} 自定义工具模块={config.custom_tool_modules}"
        )

        async def _main() -> int:
            try:
                return await _run(config)
            finally:
                # 幂等：装配上下文正常退出时已清理，这里兜住异常路径
                await close_service()

        return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
