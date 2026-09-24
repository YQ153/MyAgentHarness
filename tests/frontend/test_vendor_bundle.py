"""内置第三方发行文件的完整性、出处与豁免边界。

两件事都必须有人看着：

- **完整性 / 出处**：`static/vendor/mermaid.min.js` 是一份压缩过的第三方代码，放它入库
  意味着评审时只能看到「一行巨大的差异」。把 sha256 钉在 `vendor/README.md` 登记的值上，
  任何替换都会当场失败，逼着人回头核对来源与版本是否同步更新——这正是「静默换掉一份
  可在浏览器里执行的文件」的唯一可见点。
- **豁免边界**：`test_static_js_contract.py` 的命名契约只扫 `static/*.js`（不下钻），
  第三方文件因此不受那份契约约束（拿它去量压缩代码只会刷出一屏误报）。这条豁免是**有意
  的**，但一旦有人把那里的 glob 改成 `rglob`，误报会立刻出现——把这个前提钉在这里，
  让那次改动在本文件失败，而不是在契约文件里变成一堆看不懂的告警。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "interfaces" / "web" / "static"
VENDOR = STATIC / "vendor"
MANIFEST = VENDOR / "README.md"
BUNDLE = VENDOR / "mermaid.min.js"

_EXPECTED_VERSION = "11.12.0"
_NPM_SOURCE = "cdn.jsdelivr.net/npm/mermaid@11.12.0/dist/mermaid.min.js"


def _manifest_row(name: str) -> str:
    """取登记表里某一行的值（形如 ``| 版本 | 11.12.0 |``）。

    WHY 只认那一张表：README 里还有其它含竖线的排版段落，按行首 ``| 名字 |`` 精确匹配，
    比「搜到第一个数字」可靠——后者在表格增删一列之后会静默取到别的字段。
    """
    assert MANIFEST.is_file(), f"内置依赖的登记文件缺失：{MANIFEST}"

    text = MANIFEST.read_text(encoding="utf-8")
    match = re.search(
        rf"^\|\s*{re.escape(name)}\s*\|\s*(?P<value>.+?)\s*\|\s*$",
        text,
        re.MULTILINE,
    )
    assert match, f"{MANIFEST.name} 的登记表里没有「{name}」这一行"
    return match.group("value")


def test_manifest_records_provenance_and_license() -> None:
    """出处与许可必须写清楚：这是升级时唯一能复现「这一份从哪来」的信息。"""
    assert _EXPECTED_VERSION in _manifest_row("版本")
    assert _NPM_SOURCE in _manifest_row("来源"), "来源必须精确到版本与文件，便于复现下载"
    assert "MIT" in _manifest_row("许可"), "许可不可省略：内置代码同样受原许可约束"


def test_bundle_hash_matches_manifest() -> None:
    """哈希是本目录唯一能挡住「静默替换」的东西。"""
    digest = hashlib.sha256(BUNDLE.read_bytes()).hexdigest()

    assert digest == _manifest_row("sha256").lower(), (
        "内置发行文件的哈希与登记值不一致：\n"
        f"  实际：{digest}\n"
        f"  登记：{_manifest_row('sha256')}\n"
        "若这是有意升级，请同步更新 vendor/README.md 里的版本 / 大小 / 哈希。"
    )


def test_bundle_size_matches_manifest() -> None:
    """体积也登记一份：它顺带说明「为什么不能写进 index.html」（2.7 MB 的解析代价）。"""
    expected = re.search(r"\d+", _manifest_row("大小"))

    assert expected, "登记表里的「大小」必须是字节数"
    assert BUNDLE.stat().st_size == int(expected.group(0))


def test_bundle_exposes_global_mermaid() -> None:
    """发行文件必须以全局 ``mermaid`` 导出。

    WHY 只看结尾：这份构建（esbuild 的 IIFE）在最后一行才把模块挂到 ``globalThis``。
    下载错了构建形态（例如换成纯 ESM 的 `mermaid.core.mjs`）时，页面上的表现是
    「脚本加载成功、但全局没有 mermaid」——换一句更常见的话说就是「图全都不出来」，
    而控制台里没有任何报错。
    """
    tail = BUNDLE.read_bytes()[-4000:].decode("utf-8", errors="replace")

    assert '"mermaid"' in tail and "globalThis" in tail, (
        "发行文件结尾没有把模块挂到全局 mermaid：可能取到了非 UMD 的构建"
    )


def test_naming_contract_does_not_descend_into_vendor() -> None:
    """自研脚本的命名契约不得把第三方文件一起量了。"""
    scanned = sorted(STATIC.glob("*.js"))

    assert scanned, "glob 没命中任何脚本，下面的断言会静默通过"
    assert not [path for path in scanned if path.parent == VENDOR], (
        "static/vendor/ 下的第三方文件被卷进了自研脚本的命名契约："
        "契约检查请继续用 glob（不下钻），不要改成 rglob"
    )
    assert BUNDLE.is_file(), f"内置发行文件缺失：{BUNDLE}"


_URL_DECL = re.compile(r"""const\s+VENDOR_URL\s*=\s*'(?P<url>[^']+)'\s*;""")


def test_module_url_resolves_to_the_vendored_file() -> None:
    """模块里写死的 URL 必须真的指向静态根目录下的这份文件。

    静态挂载以 ``interfaces/web/static`` 为根（``app.py`` 的 ``StaticFiles``），因此
    ``/vendor/mermaid.min.js`` 对应的是本目录这一份。路径写错（改名、换目录、少一层）时
    服务端只会回 404，页面上表现为「图全部降级成源码 + 一行原因」——不报错、不进服务端
    日志，最难被归因到「路径」两个字。这条断言把「模块的字符串」与「磁盘上的文件」焊上。
    """
    loader = (STATIC / "mermaid_render.js").read_text(encoding="utf-8")
    match = _URL_DECL.search(loader)

    assert match, "mermaid_render.js 里找不到 VENDOR_URL 的声明（写法改了？）"

    url = match.group("url")
    assert url.startswith("/"), f"静态资源地址必须是绝对路径，当前是 {url}"
    assert (STATIC / url.lstrip("/")).is_file(), (
        f"模块里的 {url} 在静态根目录下没有对应文件：静态挂载的根是 {STATIC}"
    )


def test_vendored_bundle_is_not_eagerly_loaded() -> None:
    """首页不得直接挂载这份发行文件。

    WHY 需要这条：性能取舍写完注释就没人能再验证了。2.7 MB 的解析代价如果被写进
    `index.html` 的 `<script>`，表现是每次打开页面（包括绝大多数根本没有图的会话）
    都白付一次——而它不会报错、也不会变慢到被察觉，只会长期存在。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    loader = (STATIC / "mermaid_render.js").read_text(encoding="utf-8")

    assert "vendor/mermaid.min.js" not in html, (
        "index.html 直接引了内置发行文件：它应当由 mermaid_render.js 在第一次遇到图时按需注入"
    )
    assert "vendor/mermaid.min.js" in loader, "按需加载的那一侧必须真的指向这份内置文件"
