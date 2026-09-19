"""前端接线的静态一致性检查。

WHY 单独成文件、且不依赖 node：渲染器的安全用例需要 node，而接线检查不需要——把它
和那些用例放在同一个模块里，会让「没装 node 就整块跳过」，于是一处本来总能验证的
东西也跟着消失了。

WHY 检查挂载顺序：``app.js`` 在自己的解析阶段就会用到全局 ``Markdown``。脚本顺序
反过来时不会报错，只会表现为「所有助手消息都退化成纯文本」——而那与「模型没输出
Markdown」看起来一模一样。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "interfaces" / "web" / "static"

_ELEMENT_BY_ID = re.compile(r"""getElementById\(\s*['"](?P<id>[^'"]+)['"]\s*\)""")
_HTML_ID = re.compile(r"""\bid="(?P<id>[^"]+)\"""")


def test_every_getelementbyid_target_exists_in_html() -> None:
    """``app.js`` 引用的每个元素 id 都必须在 ``index.html`` 里真实存在。

    WHY 需要这条：``app.js`` 没有构建步骤也没有运行期类型检查，``getElementById``
    写错一个字母只返回 ``null``，随后 ``els.xxx.addEventListener`` 会在**初始化时**
    抛错——而它抛在最外层，表现是「整页都点不动」，不是「某个按钮没反应」。两者排查
    方向完全不同，现场又几乎没有可读的线索。

    WHY 与 ``test_static_js_contract`` 分开：那一条管「调用的函数声明过」（名字层面），
    这一条管「引用的元素存在」（DOM 层面）；它们挡的是两类不同的错误，合并之后
    任何一处的白名单调整都会牵动另一处。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    declared = {match.group("id") for match in _HTML_ID.finditer(html)}
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    referenced = {match.group("id") for match in _ELEMENT_BY_ID.finditer(script)}

    missing = sorted(referenced - declared)

    assert not missing, f"app.js 引用了 index.html 中不存在的 id：{missing}"


def test_index_loads_renderer_before_app() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'src="/markdown.js"' in html, "index.html 未挂载 markdown.js"
    assert html.index('src="/markdown.js"') < html.index('src="/app.js"'), (
        "markdown.js 必须排在 app.js 之前，否则 app.js 拿不到全局 Markdown"
    )


def test_static_files_exist() -> None:
    for name in ("index.html", "app.js", "markdown.js", "styles.css"):
        assert (STATIC / name).is_file(), f"静态资源缺失：{name}"


def test_app_renders_assistant_messages_through_the_renderer() -> None:
    """助手消息的两条渲染路径都必须经过渲染器，且都有纯文本兜底。

    WHY 两条都要查：历史与流式是两段独立代码，只接一条的表现是「刷新前没有排版、
    刷新后有了」——这种「半接线」用户很难描述，只能靠这里挡住。
    """
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert script.count("window.Markdown.render(") >= 2, "历史与流式两条路径都应渲染 Markdown"
    # 兜底必须是 textContent：渲染器缺席时用 innerHTML 塞原文，等于把 XSS 面又打开
    assert "body.textContent = content;" in script
