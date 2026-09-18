"""运维端点的 HTTP 契约测试。

WHY 只起一个挂了健康路由的最小应用：``create_app`` 的 lifespan 会装配真实的
数据库、模型与图，探活端点恰恰要覆盖「依赖还没装配好」的分支，用真实应用
反而测不到；同时最小应用不依赖任何 API Key，CI 也能跑。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.dto import CheckResult, MetricsSnapshot, ReadinessReport
from interfaces.web.health import router


class StubHealthService:
    """健康检查服务的替身：返回固定结果并记录调用。"""

    def __init__(
        self,
        *,
        ready: bool = True,
        checks: list[CheckResult] | None = None,
        metrics: MetricsSnapshot | None = None,
    ) -> None:
        self._ready = ready
        self._checks = checks if checks is not None else [CheckResult(name="database", ok=True)]
        self._metrics = metrics or MetricsSnapshot(
            running_threads=0,
            started_runs=0,
            pending_hitl=0,
            audit_events=0,
            uptime_seconds=1.5,
        )

    @property
    def uptime_seconds(self) -> float:
        return self._metrics.uptime_seconds

    async def readiness(self) -> ReadinessReport:
        return ReadinessReport(ready=self._ready, checks=self._checks)

    async def metrics(self) -> MetricsSnapshot:
        return self._metrics


def _build_client(service: Any | None) -> TestClient:
    """构造只挂载运维路由的测试客户端。"""
    app = FastAPI()
    app.state.health = service
    app.include_router(router)
    return TestClient(app)


def _metrics_snapshot(**overrides: Any) -> MetricsSnapshot:
    params: dict[str, Any] = {
        "running_threads": 2,
        "started_runs": 41,
        "pending_hitl": 1,
        "timed_out_runs": 3,
        "expired_hitl": 2,
        "audit_events": 128,
        "uptime_seconds": 3661.5,
    }
    params.update(overrides)
    return MetricsSnapshot(**params)


# ------------------------------------------------------------------ 存活


def test_health_returns_ok_with_uptime():
    client = _build_client(StubHealthService())

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["uptime_seconds"] == 1.5


# ------------------------------------------------------------------ 就绪


def test_ready_returns_200_when_healthy():
    client = _build_client(
        StubHealthService(
            checks=[
                CheckResult(name="database", ok=True),
                CheckResult(name="model", ok=True),
            ]
        )
    )

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert [item["name"] for item in response.json()["checks"]] == ["database", "model"]


def test_ready_returns_503_when_database_down():
    """WHY 本用例是 T4 的核心验收点：探活系统只看状态码，
    依赖不可用必须表现为 503，否则实例会被继续分发流量。"""
    client = _build_client(
        StubHealthService(
            ready=False,
            checks=[
                CheckResult(name="database", ok=False, detail="数据库不可访问：连接已关闭"),
                CheckResult(name="model", ok=True),
            ],
        )
    )

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["checks"][0]["detail"].startswith("数据库不可访问")


# ------------------------------------------------------------------ 指标


def test_metrics_returns_snapshot():
    client = _build_client(StubHealthService(metrics=_metrics_snapshot()))

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.json() == {
        "running_threads": 2,
        "started_runs": 41,
        "pending_hitl": 1,
        "timed_out_runs": 3,
        "expired_hitl": 2,
        # 并发槽位三项取 DTO 默认值：替身只填了它自己要断言的字段
        "max_concurrent_runs": 0,
        "available_run_slots": -1,
        "rejected_runs": 0,
        "audit_events": 128,
        "uptime_seconds": 3661.5,
    }


def test_metrics_allows_null_audit_events():
    client = _build_client(StubHealthService(metrics=_metrics_snapshot(audit_events=None)))

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.json()["audit_events"] is None


# ------------------------------------------------------------------ 未装配


@pytest.mark.parametrize("path", ["/health", "/ready", "/metrics"])
def test_endpoints_return_503_when_service_missing(path: str):
    client = _build_client(None)

    response = client.get(path)

    assert response.status_code == 503
    assert "健康检查服务未初始化" in response.json()["detail"]
