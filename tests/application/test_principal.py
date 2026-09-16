"""主体与权限模型的回归测试。

覆盖面：三个角色的权限矩阵全枚举、未知角色拒绝、admin 通配、
scope 校验、不可变性、匿名主体语义。
"""

from __future__ import annotations

import dataclasses

import pytest

from application.principal import (
    ANONYMOUS_PRINCIPAL,
    PERMISSIONS,
    ROLE_PERMISSIONS,
    Principal,
)


def _principal(role: str) -> Principal:
    return Principal(user_id=f"user-{role}", role=role)


# ------------------------------------------------------------------ 权限矩阵


def test_viewer_permissions_exact():
    principal = _principal("viewer")

    for permission in ROLE_PERMISSIONS["viewer"]:
        assert principal.has_permission(permission) is True

    # viewer 不在白名单内的一切权限都必须拒绝
    for permission in PERMISSIONS:
        if permission not in ROLE_PERMISSIONS["viewer"] and permission != "admin:all":
            assert principal.has_permission(permission) is False


def test_member_permissions_exact():
    principal = _principal("member")

    for permission in ROLE_PERMISSIONS["member"]:
        assert principal.has_permission(permission) is True

    for permission in PERMISSIONS:
        if permission not in ROLE_PERMISSIONS["member"] and permission != "admin:all":
            assert principal.has_permission(permission) is False


def test_admin_has_every_permission():
    principal = _principal("admin")

    # admin:all 是通配符：包括未来新增的、未登记的权限
    for permission in list(PERMISSIONS) + ["future:permission", "anything:x"]:
        assert principal.has_permission(permission) is True


def test_role_matrix_is_strict_subset_chain():
    """viewer ⊂ member：权限只能随角色升级而增加，不能交叉。"""
    assert ROLE_PERMISSIONS["viewer"] < ROLE_PERMISSIONS["member"]


def test_unknown_role_denied_everything():
    principal = _principal("ghost")

    assert principal.has_permission("thread:read") is False
    assert principal.has_permission("admin:all") is False
    assert principal.is_admin() is False


# ------------------------------------------------------------------ 边界


@pytest.mark.parametrize("invalid", ["", None, 123])
def test_has_permission_rejects_invalid_input(invalid):
    principal = _principal("member")
    assert principal.has_permission(invalid) is False


def test_has_scope():
    principal = Principal(user_id="u", scopes=frozenset({"harness:threads:read"}))

    assert principal.has_scope("harness:threads:read") is True
    assert principal.has_scope("harness:threads:write") is False


def test_principal_is_immutable():
    principal = _principal("member")
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.role = "admin"


def test_anonymous_principal_is_admin():
    # WHY 断言这一语义：auth_mode=disabled 依赖匿名管理员保持向后兼容，
    # 若有人把它降级，disabled 模式会立刻大面积 403。
    assert ANONYMOUS_PRINCIPAL.is_admin() is True
    assert ANONYMOUS_PRINCIPAL.has_permission("audit:read") is True
