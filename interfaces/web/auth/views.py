"""认证相关页面的 HTML 渲染。

WHY 独立成模块：HTML 结构与认证逻辑的变更节奏完全不同，混在一起会让
「调整页面文案」的改动出现在一个含 OIDC 密码学校验的文件里，增加评审负担。
"""

from __future__ import annotations

from application.principal import Principal

_ACTIVATE_TEMPLATE_HEAD = (
    "<!DOCTYPE html><html><head>"
    '<meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
    "<title>授权 CLI</title></head><body>"
)


def render_activate_html(
    *,
    user_code: str = "",
    error: str | None = None,
    success: str | None = None,
    principal: Principal | None = None,
) -> str:
    """渲染浏览器端 device flow 激活页。

    Args:
        user_code: 预填的用户授权码。
        error: 错误提示；存在时以红色展示。
        success: 成功提示；存在时以绿色展示。
        principal: 当前登录主体；存在时展示登录人。

    Returns:
        完整的 HTML 文档字符串。
    """
    body_parts = [
        "<h1>授权 CLI 登录</h1>",
        "<p>请输入 CLI 上显示的用户授权码，然后点击批准。</p>",
    ]
    if error:
        body_parts.append(f'<div style="color:#dc2626;margin:12px 0;">{error}</div>')
    if success:
        body_parts.append(f'<div style="color:#15803d;margin:12px 0;">{success}</div>')
    if principal:
        body_parts.append(
            f"<p>当前登录用户：<strong>{principal.display_name or principal.user_id}</strong></p>"
        )

    body_parts.extend(
        [
            '<form method="post" action="/auth/device/activate">',
            '  <label for="user_code">用户授权码</label><br/>',
            '  <input id="user_code" name="user_code" type="text" '
            f'value="{user_code}" style="text-transform:uppercase;" required/><br/><br/>',
            '  <button type="submit">批准</button>',
            "</form>",
        ]
    )

    return _ACTIVATE_TEMPLATE_HEAD + "".join(body_parts) + "</body></html>"
