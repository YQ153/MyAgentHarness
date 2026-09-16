"""认证子模块共享的 Cookie 名常量。

WHY 独立成模块：这些名字被 ``flow``（写入）与 ``session``（清理）两侧共用，
集中在一处可以避免改名时出现「写入用新名、清理用旧名」导致的 Cookie 泄漏。
"""

from __future__ import annotations

VERIFIER_COOKIE = "harness_oidc_verifier"
"""PKCE code_verifier 的临时 Cookie 名。"""

STATE_COOKIE = "harness_oidc_state"
"""OIDC state 的临时 Cookie 名，用于防 CSRF。"""

NEXT_COOKIE = "harness_oidc_next"
"""登录成功后跳转地址的临时 Cookie 名。"""

EPHEMERAL_COOKIE_PATH = "/auth/callback"
"""临时 Cookie 的 Path。

WHY 限定到回调路径：这些 Cookie 只在登录往返期间需要，限制路径可以缩小
它们被其他请求携带的范围。
"""

EPHEMERAL_COOKIE_MAX_AGE = 600
"""临时 Cookie 的有效秒数；超时后用户需重新发起登录。"""
