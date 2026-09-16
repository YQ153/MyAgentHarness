"""API Key 存储的回归测试。

覆盖面：创建（明文只返回一次）、校验（未知/空/过期/吊销/禁用）、
列出（默认隐藏已吊销）、清理（过期禁用）与参数校验。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from runtime.api_key_store import open_api_key_store


def _iso(delta: timedelta) -> str:
    """生成与存储层同格式的 UTC ISO 时间串，保证字符串比较语义正确。"""
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


async def test_create_returns_plaintext_once(tmp_path):
    async with open_api_key_store(tmp_path / "keys.db") as store:
        created = await store.create(
            role="member", scopes=["thread:read"], description="ci"
        )

        assert created["key"].startswith("harness_")
        # 高熵 key：harness_ 前缀 + 32 字符随机段
        assert len(created["key"]) == len("harness_") + 32
        assert created["key_prefix"] == created["key"][:8]
        assert created["key_id"]
        assert created["role"] == "member"
        assert created["scopes"] == ["thread:read"]
        assert created["created_at"]


async def test_validate_roundtrip_returns_metadata_without_hash(tmp_path):
    async with open_api_key_store(tmp_path / "keys.db") as store:
        created = await store.create(role="member")

        record = await store.validate(created["key"])

        assert record is not None
        assert record["key_id"] == created["key_id"]
        assert record["role"] == "member"
        # 哈希绝不能出现在校验结果里，避免旁路泄漏
        assert "key_hash" not in record


async def test_validate_rejects_unknown_and_empty_key(tmp_path):
    async with open_api_key_store(tmp_path / "keys.db") as store:
        assert await store.validate("harness_not_a_real_key") is None
        assert await store.validate("") is None


async def test_revoke_blocks_validation_and_missing_returns_false(tmp_path):
    async with open_api_key_store(tmp_path / "keys.db") as store:
        created = await store.create(role="member")

        assert await store.revoke(created["key_id"]) is True
        assert await store.validate(created["key"]) is None
        # 语义约定：False 仅表示 key_id 不存在；对已吊销 key 重复吊销
        # 仍返回 True（行匹配即成功，天然幂等，不存在「吊销被撤销」窗口）
        assert await store.revoke(created["key_id"]) is True
        assert await store.revoke("missing-key-id") is False


async def test_expired_key_rejected(tmp_path):
    async with open_api_key_store(tmp_path / "keys.db") as store:
        created = await store.create(role="member", expires_at=_iso(timedelta(hours=-1)))
        assert await store.validate(created["key"]) is None


async def test_future_expiry_still_valid(tmp_path):
    async with open_api_key_store(tmp_path / "keys.db") as store:
        created = await store.create(role="member", expires_at=_iso(timedelta(hours=1)))
        assert await store.validate(created["key"]) is not None


async def test_invalid_expiry_treated_as_expired(tmp_path):
    """WHY 关键安全语义：非法过期时间必须按已过期处理，不允许永不过期漏洞。"""
    async with open_api_key_store(tmp_path / "keys.db") as store:
        created = await store.create(role="member", expires_at="not-a-date")
        assert await store.validate(created["key"]) is None


async def test_list_hides_revoked_by_default(tmp_path):
    async with open_api_key_store(tmp_path / "keys.db") as store:
        kept = await store.create(role="member")
        revoked = await store.create(role="viewer")
        await store.revoke(revoked["key_id"])

        visible = await store.list()
        assert [item["key_id"] for item in visible] == [kept["key_id"]]

        everything = await store.list(include_revoked=True)
        assert {item["key_id"] for item in everything} == {
            kept["key_id"],
            revoked["key_id"],
        }


async def test_cleanup_expired_disables_keys(tmp_path):
    async with open_api_key_store(tmp_path / "keys.db") as store:
        expired = await store.create(role="member", expires_at=_iso(timedelta(hours=-1)))
        alive = await store.create(role="member", expires_at=_iso(timedelta(hours=1)))

        cleaned = await store.cleanup_expired()

        assert cleaned == 1
        assert await store.validate(expired["key"]) is None
        assert await store.validate(alive["key"]) is not None


async def test_create_empty_role_rejected(tmp_path):
    async with open_api_key_store(tmp_path / "keys.db") as store:
        with pytest.raises(ValueError):
            await store.create(role="")


async def test_revoke_empty_key_id_rejected(tmp_path):
    async with open_api_key_store(tmp_path / "keys.db") as store:
        with pytest.raises(ValueError):
            await store.revoke("")
