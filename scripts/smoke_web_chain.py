"""真机冒烟：联网「检索 → 抓取」完整链路（T14 遗留验收）。

背景：T14 的交付物（检索 / 抓取工具 + 出站安全）已通过单测与一次真机**抓取**
验收，但「检索 → 抓取」整条链路一直没有在真实检索服务上跑过——当时本机没有
可用的 tavily 密钥或 SearXNG 实例。本脚本把这次验收固化成可复跑的形式：配好
provider 后直接运行即可；未配置时以退出码 ``2`` 明确跳过并说明怎么启用，
而不是伪装成通过。

WHY 不经过模型：要问的是「工具链路是否通」，而不是「模型会不会调用它们」。
混进模型后，「链路断了」与「模型没按预期调用」会变成同一个失败，排查方向被
误导。抓取到的正文也不写盘——落文件那一段已由 T14 的 Agent 端到端验收覆盖。

用法：
    python scripts/smoke_web_chain.py              # 用默认查询词
    python scripts/smoke_web_chain.py "关键词"      # 指定查询词

退出码：``0`` 全通 / ``2`` 有跳过（未配置 provider）/ ``1`` 有失败。
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
# WHY 强制 UTF-8 输出：Windows 控制台默认 GBK，网页标题含非 GBK 字符时 print
# 会抛 UnicodeEncodeError，把冒烟结论变成一条假失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import AppConfig  # noqa: E402
from agent.tools import ToolRegistry  # noqa: E402
from web_tools import WebToolError, _search_unavailable_reason, register_tools  # noqa: E402

logger = logging.getLogger("smoke.web_chain")

_DEFAULT_QUERY = "python asyncio 官方文档"
_LINK_RE = re.compile(r"链接：\s*(\S+)")
"""从检索工具的渲染文本里取链接。

WHY 解析自家渲染格式而不是绕过工具直接调适配器：本脚本要验证的正是「工具」
这一层——纹理（编号 / 标题 / 链接 / 摘要）本身也是模型依赖的约定，走适配器
就把这一半排除在验收之外了。
"""


async def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    query = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else _DEFAULT_QUERY

    config = AppConfig(_env_file=str(ROOT / ".env") if (ROOT / ".env").exists() else None)
    registry = ToolRegistry()
    register_tools(registry, config)
    tools = {item.name: item for item in registry.tools()}

    reason = _search_unavailable_reason(config)
    if reason is not None or "web_search" not in tools:
        print("[SKIP] 检索工具未注册：", reason or "工具清单里没有 web_search")
        print("       启用方式（二选一，然后重跑本脚本）：")
        print("         tavily : WEB_SEARCH_PROVIDER=tavily 且 WEB_SEARCH_API_KEY=tvly-...")
        print("         searxng: WEB_SEARCH_PROVIDER=searxng 且 WEB_SEARCH_BASE_URL=http://<host>:<port>")
        print("                  （实例需在 settings.yml 的 search.formats 里开启 json）")
        print("       另需 CUSTOM_TOOL_MODULES=web_tools 才会加载本模块。")
        return 2

    print(f"=== 检索：{query} ===")
    try:
        search_output = await tools["web_search"].ainvoke({"query": query})
    except WebToolError as exc:
        print(f"[FAIL] 检索失败：{type(exc).__name__}: {exc}")
        return 1

    links = _LINK_RE.findall(search_output)
    print(search_output[:1200])
    if not links:
        print("[FAIL] 检索返回了结果文本，但其中没有可抓取的链接。")
        return 1

    target = links[0]
    print(f"\n=== 抓取首条结果：{target} ===")
    try:
        text = await tools["web_fetch"].ainvoke({"url": target})
    except WebToolError as exc:
        print(f"[FAIL] 抓取失败：{type(exc).__name__}: {exc}")
        return 1

    if not text.strip():
        print("[FAIL] 抓取返回空正文。")
        return 1

    print(text[:800])
    print(f"\n[PASS] 链路打通：检索命中 {len(links)} 条，抓取首条得到 {len(text)} 字符。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
