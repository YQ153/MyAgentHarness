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

from agent.tooling import build_tool_bundle  # noqa: E402
from bootstrap.core import build_app_context  # noqa: E402
from config import AppConfig  # noqa: E402
from knowledge_runtime import (  # noqa: E402
    close_service,
    ensure_service,
    knowledge_db_path,
    peek_service,
)
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


async def _run(config: AppConfig) -> int:
    """装配 → 索引 → 检索，返回退出码。"""
    config.workspace.mkdir(parents=True, exist_ok=True)
    (config.workspace / "login.md").write_text(_DOC, encoding="utf-8")

    print("[1/5] 经 build_app_context 装配（与 CLI / Web 启动同一条路径）")
    async with build_app_context(config) as context:
        capabilities = context.knowledge.capabilities()
        print(f"      AppContext.knowledge 就位：{knowledge_db_path(config)}")
        print(
            f"      向量检索={capabilities['vector_enabled']} "
            f"嵌入={capabilities['embedding_backend']}"
        )
        if peek_service() is not context.knowledge:
            print("[FAIL] 工具侧句柄与 AppContext 里的不是同一个实例")
            return 1

        print("[2/5] 索引工作区")
        summary = await context.knowledge.index_workspace()
        print(
            f"      扫描 {summary['scanned']}，新索引 {summary['indexed']}，"
            f"未变化 {summary['unchanged']}，跳过 {summary['skipped']}"
        )
        if summary["indexed"] != 1:
            print(f"[FAIL] 期望索引 1 个文件，实际 {summary['indexed']}")
            return 1

        print(f"[3/5] 服务层检索：{_QUERY!r}")
        direct = await context.knowledge.search(_QUERY)
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
        rendered = await search_tool.ainvoke({"query": _QUERY})
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
        config = AppConfig(
            _env_file=root / "no-such.env",
            workspace=root / "workspace",
            memory_file=root / "workspace" / "AGENTS.md",
            db_path=root / "data" / "agent.db",
            skill_dirs=[root / "workspace" / "skills"],
            auth_mode="disabled",
            embedding_backend=args.backend,
            embedding_python=python,
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
