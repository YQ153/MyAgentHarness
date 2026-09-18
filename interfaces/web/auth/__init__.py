"""Web 端认证与授权子包。

职责边界：只处理 HTTP 层面的身份提取、API Key 管理与审计，不持有业务权限规则
（规则在 ``application.principal``）。

WHY 只有 API Key 一种凭据：本服务的使用者是「持有密钥的调用方」（CLI、脚本、集成方），
而不是需要自助注册与找回密码的终端用户。把外部 IdP 接进来会引入 issuer 可达性、
Cookie 会话、跨站跳转这一整套与之无关的复杂度，而它换来的能力这里并不需要。

对外只暴露与原 ``auth.py`` 相同的符号，调用方导入路径无需修改：

    from interfaces.web.auth import router            # 路由
    from interfaces.web.auth import get_principal     # 身份提取依赖
    from interfaces.web.auth import require_permission  # 权限依赖

子模块划分：

- ``utils``        请求头解析
- ``audit``        审计事件写入
- ``deps``         身份提取与权限校验
- ``identity``     登出与身份查询路由
- ``apikey``       API Key 管理端点
- ``audit_api``    审计查询端点
- ``router``       路由汇总
"""

from interfaces.web.auth.deps import get_principal, require_permission
from interfaces.web.auth.router import router

__all__ = ["get_principal", "require_permission", "router"]
