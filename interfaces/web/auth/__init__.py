"""Web 端认证与授权子包。

职责边界：只处理 HTTP 层面的身份提取、会话管理、OIDC 往返、API Key 与
设备授权流程，不持有业务权限规则（规则在 ``application.principal``）。

对外只暴露与原 ``auth.py`` 相同的符号，调用方导入路径无需修改：

    from interfaces.web.auth import router            # 路由
    from interfaces.web.auth import get_principal     # 身份提取依赖
    from interfaces.web.auth import require_permission  # 权限依赖

子模块划分：

- ``constants``    Cookie 名与有效期
- ``utils``        请求头解析与跳转目标校验
- ``session``      会话 Cookie 读写
- ``audit``        审计事件写入
- ``oidc``         OIDC 协议交互
- ``deps``         身份提取与权限校验
- ``views``        HTML 渲染
- ``flow``         登录与回调路由（OIDC 授权码往返）
- ``identity``     登出与身份查询路由
- ``apikey``       API Key 管理端点
- ``audit_api``    审计查询端点
- ``device_flow``  Device Authorization 端点
- ``router``       路由汇总
"""

from interfaces.web.auth.deps import get_principal, require_permission
from interfaces.web.auth.router import router

__all__ = ["get_principal", "require_permission", "router"]
