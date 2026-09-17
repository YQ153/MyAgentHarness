"""/api/usage 的 HTTP 契约测试。

WHY 只挂业务路由而不起真实应用：``create_app`` 的 lifespan 会装配数据库、
模型与图，而用量端点要覆盖的是「查询参数翻译与状态码映射」，与真实依赖无关。

WHY 存储用替身而不是真库：``TestClient`` 会在自己的事件循环里跑应用，
测试侧另建的连接与锁跨循环使用必然报错；替身把断言聚焦在契约本身。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.errors import NotFoundError, OwnershipError
from application.usage_service import UsageService
from config import AppConfig
from interfaces.web.routes import router
from tests.application.test_usage_service import StubThreadStore
from tests.conftest import make_config

_CANNED_SUMMARY: dict[str, Any] = {
    "prompt_tokens": 10,
    "completion_tokens": 2,
    "total_tokens": 12,
    "run_count": 1,
    "groups": [
        {
            "key": "deepseek-flash",
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
            "run_count": 1,
        }
    ],
}


class StubUsageStore:
    """用量存储替身：记录入参并返回固定聚合结果。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def summarize(
        self,
        *,
        owner_id: str | None = None,
        thread_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        group_by: str = "model",
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "owner_id": owner_id,
                "thread_id": thread_id,
                "since": since,
                "until": until,
                "group_by": group_by,
            }
        )
        if self.error is not None:
            raise self.error
        return _CANNED_SUMMARY


def _build_client(
    tmp_path,
    usage_service: UsageService | None,
) -> TestClient:
    app = FastAPI()
    app.state.config = make_config(tmp_path)
    app.state.usage = usage_service
    app.include_router(router)
    return TestClient(app)


def _service(tmp_path, store: StubUsageStore, **overrides: Any) -> UsageService:
    return UsageService(
        make_config(tmp_path, **overrides),
        usage_store=store,
        thread_store=StubThreadStore({"t1": {"thread_id": "t1", "owner_id": "alice"}}),
    )


# ------------------------------------------------------------------ 正常路径


def test_usage_endpoint_returns_summary(tmp_path):
    store = StubUsageStore()
    client = _build_client(tmp_path, _service(tmp_path, store))

    response = client.get("/api/usage?days=7&group_by=model")

    assert response.status_code == 200
    assert response.json() == {
        "window_days": 7,
        "since": store.calls[0]["since"],
        "group_by": "model",
        "thread_id": None,
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
        "run_count": 1,
        "groups": [
            {
                "key": "deepseek-flash",
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "run_count": 1,
            }
        ],
    }
    # 时间窗必须真的传下去，否则「7 天」只是个回显
    assert store.calls[0]["since"] < store.calls[0]["since"][:10] + "T23:59:59"


def test_usage_endpoint_uses_config_default_window(tmp_path):
    store = StubUsageStore()
    client = _build_client(tmp_path, _service(tmp_path, store, usage_default_window_days=3))

    response = client.get("/api/usage")

    assert response.status_code == 200
    assert response.json()["window_days"] == 3


def test_usage_endpoint_passes_thread_and_group_by(tmp_path):
    store = StubUsageStore()
    client = _build_client(tmp_path, _service(tmp_path, store))

    response = client.get("/api/usage?thread_id=t1&days=1&group_by=day")

    assert response.status_code == 200
    assert response.json()["thread_id"] == "t1"
    assert store.calls[0]["thread_id"] == "t1"
    assert store.calls[0]["group_by"] == "day"
    # auth_mode=disabled：不过滤 owner
    assert store.calls[0]["owner_id"] is None


# ------------------------------------------------------------------ 错误映射


@pytest.mark.parametrize(
    "error,status_code,fragment",
    [
        (ValueError("days 必须在 1..90 之间"), 400, "days"),
        (NotFoundError("会话", "t1"), 404, "会话"),
        (OwnershipError("会话", "t1"), 403, "会话"),
        (RuntimeError("用量聚合失败"), 500, "用量聚合失败"),
    ],
)
def test_usage_endpoint_maps_errors(tmp_path, error: Exception, status_code: int, fragment: str):
    """WHY 逐条覆盖状态码映射：该端点同时存在「用户传错」「无权」与
    「服务故障」三类失败，映射错一个就会让前端把 500 当成空数据。"""
    client = _build_client(tmp_path, _service(tmp_path, StubUsageStore(error=error)))

    response = client.get("/api/usage")

    assert response.status_code == status_code
    assert fragment in response.json()["detail"]


def test_usage_endpoint_returns_503_when_service_missing(tmp_path):
    client = _build_client(tmp_path, None)

    response = client.get("/api/usage")

    assert response.status_code == 503
    assert "用量统计服务未初始化" in response.json()["detail"]


def test_usage_endpoint_rejects_negative_days_before_service(tmp_path):
    """WHY 路由层就拦截 ``days<1``：非法值不应打到服务与数据库。"""
    client = _build_client(tmp_path, _service(tmp_path, StubUsageStore()))

    response = client.get("/api/usage?days=0")

    assert response.status_code == 422
