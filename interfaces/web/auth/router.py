"""认证子包的路由汇总。

WHY 在这里统一挂前缀：各子模块只声明自己相对 ``/auth`` 的路径，
前缀只在一处出现，避免修改挂载点时遗漏某个子路由。
"""

from __future__ import annotations

from fastapi import APIRouter

from interfaces.web.auth.apikey import router as apikey_router
from interfaces.web.auth.audit_api import router as audit_api_router
from interfaces.web.auth.identity import router as identity_router

router = APIRouter(prefix="/auth", tags=["auth"])
router.include_router(identity_router)
router.include_router(apikey_router)
router.include_router(audit_api_router)

__all__ = ["router"]
