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
    for name in (
        "index.html",
        "app.js",
        "markdown.js",
        "mermaid_render.js",
        "workspace_scope.js",
        "styles.css",
    ):
        assert (STATIC / name).is_file(), f"静态资源缺失：{name}"


def test_index_loads_mermaid_module_before_app() -> None:
    """Mermaid 后处理模块必须排在 ``app.js`` 之前。

    WHY 与前面两条同一类接线错误：``app.js`` 在渲染助手消息时直接读全局
    ``MermaidView``。脚本错序时不会报错（读到的只是 ``undefined``，而调用点本来就为
    「渲染器缺席」留了分支），表现是「图全都不画、代码块倒是都在」——那与「模型没写
    mermaid 代码块」在界面上完全一样。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'src="/mermaid_render.js"' in html, "index.html 未挂载 mermaid_render.js"
    assert html.index('src="/mermaid_render.js"') < html.index('src="/app.js"'), (
        "mermaid_render.js 必须排在 app.js 之前，否则 app.js 拿不到全局 MermaidView"
    )


def test_app_renders_assistant_messages_through_the_renderer() -> None:
    """助手消息的两条渲染路径都必须经过渲染器，且都有纯文本兜底。

    WHY 两条都要查：历史与流式是两段独立代码，只接一条的表现是「刷新前没有排版、
    刷新后有了」——这种「半接线」用户很难描述，只能靠这里挡住。
    """
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert script.count("window.Markdown.render(") >= 2, "历史与流式两条路径都应渲染 Markdown"
    # 兜底必须是 textContent：渲染器缺席时用 innerHTML 塞原文，等于把 XSS 面又打开
    assert "body.textContent = content;" in script


def test_both_message_paths_enhance_diagrams() -> None:
    """助手消息的两条渲染路径都必须触发图渲染，且缺席时不影响正文。

    WHY 两条都要查：历史与流式是两段独立代码，只接一条的表现是「刷新前有图、刷新后
    变成一堆代码块」——用户很难描述这种半接线，只会以为图渲染不可靠。

    WHY 还要查那一层守卫：这段逻辑依赖一个**第三方**资源的接缝，它不在页面上时报错，
    只在控制台里。守卫缺席时，一次「内置发行文件没随部署带过去」会把所有助手消息
    变成空白——连文字都没了，而那是最难被归因到「图」的一件事。
    """
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    for name in ("renderHistory", "handleEvent"):
        assert "enhanceDiagrams(" in _function_source(script, name), (
            f"{name} 未触发图渲染"
        )

    helper = _function_source(script, "enhanceDiagrams")
    assert "window.MermaidView" in helper, "缺少「渲染模块不在」的守卫"
    assert "catch(" in helper, (
        "未处理 enhance 的拒绝：一次未捕获的拒绝会静默中断这一轮的收尾"
    )
    # 兜底必须是 textContent（与 Markdown 那条同一理由）；图渲染绝不能反过来去动正文
    assert "innerHTML" not in helper, "图渲染不应改写正文的 innerHTML"


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


def test_session_list_groups_by_workspace_and_offers_deletion() -> None:
    """会话清单必须按工作空间分组，且单条与整组都有删除入口。

    WHY 需要这条：这两件事**只**在界面上接线（后端端点各有专门用例），漏接的表现是
    「按钮点了没反应」或「清单还是平铺的」——两者都不抛错、也不进日志，只能靠人点开
    界面才发现。这里挡的是最粗的那一类：入口压根不存在。
    """
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "groupThreads(" in _function_source(script, "renderThreads"), (
        "renderThreads 未按工作空间分组"
    )
    assert "deleteThread(item)" in _function_source(script, "threadActions"), (
        "会话条目缺少删除入口"
    )
    group_body = _function_source(script, "renderThreadGroup")
    assert "deleteWorkspaceGroup(group)" in group_body, "分组标题缺少「删除该工作空间」入口"
    assert "workspaceLabel(" in _function_source(script, "groupThreads"), (
        "分组标题必须显示工作空间短名，否则只是一行路径"
    )


def test_workspace_group_deletion_covers_archived() -> None:
    """整组删除必须显式要求包含已归档的会话。

    WHY 需要这条：归档只是清单可见性，而「删除这个工作空间」的语义是「把它清空」。漏掉
    这个参数时请求依然成功（删掉的是当前可见的那几条），用户下次勾上「含已归档」会看到
    这一组又回来了——而他会以为是删除没生效。
    """
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    body = _function_source(script, "deleteWorkspaceGroup")

    assert "include_archived" in body, "整组删除未包含已归档的会话"
    assert "bound" in body, "整组删除必须区分「绑定到某目录」与「未绑定」两种形态"


def test_usage_panel_is_wired_to_both_viewpoints() -> None:
    """用量面板的两个视角都必须真的取数、渲染，并把缓存命中率显出来。

    WHY 需要这条：面板是纯接线（端点与聚合各有专门用例）。漏接的表现是「按钮点了没
    反应」或「数字永远不动」——两者都不抛错、也不进日志，只能靠人点开界面才发现。

    WHY 单独盯命中率：它是这个面板存在的理由——「DeepSeek 的缓存到底有没有收益」只能
    由它回答。面板漏渲染它，判断依据就又回到了只能靠猜的状态（这正是本次修复的起点）。

    WHY 两个视角都要断言：它们的取数端点不同，只接一个的表现是「切过去一片空白」，
    而用户会先怀疑「这段时间没有用量」——归因方向完全错了。
    """
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    aggregate_body = _function_source(script, "loadUsageAggregate")
    assert "/api/usage?" in aggregate_body, "聚合视角未调用用量端点"
    assert "group_by" in aggregate_body, "聚合视角未传分组维度，切换分组不会生效"

    series_body = _function_source(script, "loadUsageSeries")
    assert "/api/usage/series" in series_body, "按次视角未调用序列端点"
    assert "limit" in series_body, "按次视角未传条数上限"

    # 分派器必须按视角选端点，否则切换视角只是把同一份数据又取了一遍
    load_body = _function_source(script, "loadUsage")
    assert "usageView()" in load_body, "loadUsage 未读取视角"
    assert "loadUsageSeries()" in load_body and "loadUsageAggregate()" in load_body, (
        "loadUsage 未按视角分派到两个加载器"
    )

    for name in ("renderUsage", "renderUsageSeries"):
        body = _function_source(script, name)
        assert "cache_hit_rate" in body, f"{name} 未渲染缓存命中率"
        assert "formatHitRate(" in body, f"{name} 命中率未经格式化：否则「未知」会显示成 0%"

    # 「未知」与 0% 是两个相反结论，格式化函数必须显式区分它们
    rate_body = _function_source(script, "formatHitRate")
    assert "未知" in rate_body, "命中率格式化未区分「provider 未上报」与「确实没命中」"

    # 按次视角的「趋势」靠内联条承载，漏画它就退化成一列数字
    assert "hitRateBar(" in _function_source(script, "renderUsageSeries"), (
        "按次视角未画命中率条——趋势会退化成一列数字"
    )

    assert "openUsageModal" in _function_source(script, "bindUsageEvents"), (
        "用量入口按钮没有绑定打开事件"
    )
    assert "bindUsageEvents()" in _function_source(script, "init"), (
        "init 未绑定用量面板事件——按钮会一直没反应"
    )
