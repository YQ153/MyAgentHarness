"""内存级滑动窗口限流器的回归测试。

覆盖面：构造参数校验、窗口内计数与封禁、多 key 隔离、窗口过期重置、
手动重置与过期桶清理。时间相关用例用极短真实窗口（约 50ms）驱动，
不 mock 时钟，避免对 ``time.monotonic`` 的实现细节形成耦合。
"""

from __future__ import annotations

import time

import pytest

from runtime.rate_limiter import RateLimiter


def test_constructor_rejects_nonpositive_params():
    with pytest.raises(ValueError):
        RateLimiter(window_seconds=0, max_attempts=5)
    with pytest.raises(ValueError):
        RateLimiter(window_seconds=-1, max_attempts=5)
    with pytest.raises(ValueError):
        RateLimiter(window_seconds=60, max_attempts=0)


def test_allows_up_to_max_then_blocks():
    limiter = RateLimiter(window_seconds=60, max_attempts=3)

    assert [limiter.is_allowed("ip-a") for _ in range(3)] == [True, True, True]
    assert limiter.is_allowed("ip-a") is False


def test_keys_are_independent():
    limiter = RateLimiter(window_seconds=60, max_attempts=1)

    assert limiter.is_allowed("ip-a") is True
    assert limiter.is_allowed("ip-a") is False
    assert limiter.is_allowed("ip-b") is True


def test_window_expiry_re_allows():
    limiter = RateLimiter(window_seconds=0.05, max_attempts=1)

    assert limiter.is_allowed("ip-a") is True
    assert limiter.is_allowed("ip-a") is False

    time.sleep(0.06)
    assert limiter.is_allowed("ip-a") is True


def test_reset_re_allows_immediately():
    limiter = RateLimiter(window_seconds=60, max_attempts=1)

    assert limiter.is_allowed("ip-a") is True
    assert limiter.is_allowed("ip-a") is False

    limiter.reset("ip-a")
    assert limiter.is_allowed("ip-a") is True


def test_reset_unknown_key_is_noop():
    limiter = RateLimiter(window_seconds=60, max_attempts=1)
    limiter.reset("never-seen")
    assert limiter.is_allowed("never-seen") is True


def test_cleanup_removes_only_expired_buckets():
    limiter = RateLimiter(window_seconds=0.05, max_attempts=10)

    limiter.is_allowed("expired-ip")
    limiter.is_allowed("fresh-ip")
    time.sleep(0.06)
    limiter.is_allowed("fresh-ip")  # 刷新 fresh-ip 的窗口起点

    removed = limiter.cleanup()

    assert removed == 1
    # 清理后的 key 重新计数，而不是延续旧窗口
    assert limiter.is_allowed("expired-ip") is True


def test_non_string_key_rejected():
    limiter = RateLimiter(window_seconds=60, max_attempts=1)
    assert limiter.is_allowed(None) is False
    assert limiter.is_allowed(12345) is False
