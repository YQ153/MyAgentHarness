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

from application.errors import NotFoundError
from application.usage_service import UsageService
from config import AppConfig
from interfaces.web.routes import router
from tests.application.test_usage_service import StubThreadStore
from tests.conftest import make_config

_CANNED_SUMMARY: dict[str, Any] = {
    "prompt_tokens": 10,
    "completion_tokens": 2,
    "total_tokens": 12,
    "cache_hit_tokens": 8,
    "cache_miss_tokens": 2,
    "call_count": 1,
    "groups": [
        {
            "key": "deepseek-flash",
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
            "cache_hit_tokens": 8,
            "cache_miss_tokens": 2,
            "call_count": 1,
        }
    ],
}


_CANNED_SERIES: list[dict[str, Any]] = [
    {
        "id": 1,
        "thread_id": "t1",
        "model": "deepseek-flash",
        "prompt_tokens": 1000,
        "completion_tokens": 10,
        "cache_hit_tokens": 900,
        "cache_miss_tokens": 0,
        "created_at": "2026-09-23T08:12:23+00:00",
    },
    {
        "id": 2,
        "thread_id": "t1",
        "model": "deepseek-flash",
        "prompt_tokens": 2000,
        "completion_tokens": 20,
        "cache_hit_tokens": 1000,
        "cache_miss_tokens": 0,
        "created_at": "2026-09-23T08:12:41+00:00",
    },
]


class StubUsageStore:
    """用量存储替身：记录入参并返回固定结果。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.series_calls: list[dict[str, Any]] = []

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

    async def list_recent(
        self,
        *,
        owner_id: str | None = None,
        thread_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], bool]:
        self.series_calls.append(
            {
                "owner_id": owner_id,
                "thread_id": thread_id,
                "since": since,
                "until": until,
                "limit": limit,
            }
        )
        if self.error is not None:
            raise self.error
        return _CANNED_SERIES, False


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
        "cache_hit_tokens": 8,
        "cache_miss_tokens": 2,
        "cache_hit_rate": 0.8,
        "call_count": 1,
        "groups": [
            {
                "key": "deepseek-flash",
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "cache_hit_tokens": 8,
                "cache_miss_tokens": 2,
                "cache_hit_rate": 0.8,
                "call_count": 1,
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
    # 不区分主体：不过滤 owner
    assert store.calls[0]["owner_id"] is None


# ------------------------------------------------------------------ 按次视角


def test_usage_series_endpoint_returns_chronological_calls(tmp_path):
    store = StubUsageStore()
    client = _build_client(tmp_path, _service(tmp_path, store))

    response = client.get("/api/usage/series?days=7&limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["limit"] == 2
    assert payload["count"] == 2
    assert payload["truncated"] is False
    # 正序：先发生的在前，趋势才读得出来
    assert [item["prompt_tokens"] for item in payload["items"]] == [1000, 2000]
    assert [item["cache_hit_rate"] for item in payload["items"]] == [0.9, 0.5]
    # 逐条记录必须带上命中数，否则前端只能显示「未知」
    assert payload["items"][0]["cache_hit_tokens"] == 900
    assert store.series_calls[0]["limit"] == 2
    # 时间窗必须真的传下去，否则「7 天」只是个回显
    assert store.series_calls[0]["since"] < store.series_calls[0]["since"][:10] + "T23:59:59"


def test_usage_series_endpoint_uses_default_limit(tmp_path):
    """不传 limit 时由服务层补默认值，而不是把 ``None`` 透传到存储层。"""
    store = StubUsageStore()
    client = _build_client(tmp_path, _service(tmp_path, store))

    response = client.get("/api/usage/series?thread_id=t1&days=1")

    assert response.status_code == 200
    assert response.json()["thread_id"] == "t1"
    assert store.series_calls[0]["thread_id"] == "t1"
    assert store.series_calls[0]["limit"] == 50


def test_usage_series_endpoint_rejects_limit_above_cap(tmp_path):
    """WHY 在服务层拦而不是路由层：CLI 与测试也走服务层，同一条上限必须对
    所有入口生效；这条断言确认「超过上限」真的到不了存储层。"""
    store = StubUsageStore()
    client = _build_client(tmp_path, _service(tmp_path, store))

    response = client.get("/api/usage/series?limit=201")

    assert response.status_code == 400
    assert store.series_calls == []


@pytest.mark.parametrize("limit", [0, -1])
def test_usage_series_endpoint_rejects_nonpositive_limit(tmp_path, limit: int):
    client = _build_client(tmp_path, _service(tmp_path, StubUsageStore()))

    response = client.get(f"/api/usage/series?limit={limit}")

    assert response.status_code == 422


@pytest.mark.parametrize(
    "error,status_code,fragment",
    [
        (ValueError("limit 必须在 1..200 之间"), 400, "limit"),
        (NotFoundError("会话", "t1"), 404, "会话"),
        (RuntimeError("用量序列查询失败"), 500, "用量序列查询失败"),
    ],
)
def test_usage_series_endpoint_maps_errors(tmp_path, error: Exception, status_code: int, fragment: str):
    client = _build_client(tmp_path, _service(tmp_path, StubUsageStore(error=error)))

    response = client.get("/api/usage/series")

    assert response.status_code == status_code
    assert fragment in response.json()["detail"]


# ------------------------------------------------------------------ 错误映射


@pytest.mark.parametrize(
    "error,status_code,fragment",
    [
        (ValueError("days 必须在 1..90 之间"), 400, "days"),
        (NotFoundError("会话", "t1"), 404, "会话"),
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
