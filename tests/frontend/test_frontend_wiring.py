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


def test_index_loads_workspace_scope_before_app() -> None:
    """工作区作用域模块必须也排在 ``app.js`` 之前。

    WHY 需要这条：``app.js`` 在**自己的解析阶段**就调用 ``WorkspaceScope.begin`` 来构造
    状态（不是等到某个事件里），错序会直接抛 ``ReferenceError``——表现是整页白屏，
    而不是「面板有点不对」。这与 Markdown 那一条是同一类接线错误，只是后果更硬。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'src="/workspace_scope.js"' in html, "index.html 未挂载 workspace_scope.js"
    assert html.index('src="/workspace_scope.js"') < html.index('src="/app.js"'), (
        "workspace_scope.js 必须排在 app.js 之前，否则 app.js 拿不到全局 WorkspaceScope"
    )


def test_static_files_exist() -> None:
    for name in ("index.html", "app.js", "markdown.js", "workspace_scope.js", "styles.css"):
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


def _function_source(script: str, name: str) -> str:
    """取出 ``function name(...) { ... }`` 的完整源码（按花括号配平）。

    WHY 配平而不是「取到下一个 function 为止」：函数体里嵌着箭头函数与对象字面量，
    简单截断的位置会随实现漂移——那样断言就会时而看见、时而看不见那次调用，
    而一条结果取决于排版位置的断言很快就没人看。

    WHY 先跳过形参表：形参里可以有自己的花括号（``openThread(id, { force = false } = {})``
    就是如此），从函数名后的第一个 ``{`` 开始配平会立刻在形参的 ``}`` 上收尾。

    WHY 不解析字符串与注释：这里只取两个特定函数的体，而它们不含「不平衡的花括号」
    （模板插值 ``${}`` 本身是配平的）。真出现了那种代码，本函数会以「没有配平」失败，
    而不是静默给出一段被截断的源码——失败方向是安全的。
    """
    match = re.search(rf"\bfunction {re.escape(name)}\s*\(", script)
    assert match, f"app.js 里找不到函数 {name}（被改名或删除了？）"

    # 1) 配平形参表，定位函数体的起始花括号
    depth = 0
    body_start = None
    for index in range(match.end() - 1, len(script)):
        char = script[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                body_start = script.find("{", index)
                break
    assert body_start != -1, f"函数 {name} 的形参表没有配平"

    # 2) 配平函数体
    depth = 0
    for index in range(body_start, len(script)):
        char = script[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return script[match.start() : index + 1]
    raise AssertionError(f"函数 {name} 的花括号没有配平")


def test_both_session_switch_paths_reset_the_workspace_scope() -> None:
    """换会话的两条路径（打开既有会话、退回草稿态）都必须同步工作区作用域。

    WHY 需要这条：工作区面板的目录树是按**虚拟路径**缓存的，而 ``/`` 在每条会话下都
    合法却指向不同目录。漏掉同步的表现是「面板左上角写着新会话的路径、右边列着上一条
    会话的文件」——不报错，且只在「先开过 A 的面板再切到 B」这种顺序下出现，用户很难
    描述清楚。这正是本次报的「切换会话时工作区没有同步切换」。
    """
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    for name in ("openThread", "startDraft"):
        body = _function_source(script, name)
        assert "syncWorkspaceScope()" in body, (
            f"{name} 未同步工作区作用域——面板会继续列着上一条会话的目录"
        )
