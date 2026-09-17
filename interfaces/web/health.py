"""运维端点：存活、就绪与运行指标。

约定：
- 三个端点都**不设鉴权**：调用方是负载均衡、容器编排与采集系统，它们通常
  拿不到业务凭据。因此响应里只有计数，不含任何会话 ID 或用户信息。
- 路径不带 ``/api`` 前缀：探活地址是基础设施约定（``/health``、``/ready``），
  加业务前缀会让运维配置与框架耦合。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from application.dto import MetricsSnapshot, ReadinessReport
from application.health import HealthService
from interfaces.web.deps import require_state
from interfaces.web.schemas import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])


def get_health(request: Request) -> HealthService:
    """取出健康检查服务单例。"""
    return require_state(request, "health", "健康检查服务")


@router.get("/health", response_model=HealthResponse)
async def health(service: HealthService = Depends(get_health)) -> HealthResponse:
    """存活探测：只要进程还能响应就返回 200。

    WHY 不检查任何依赖：存活与就绪必须分开。若存活探测也查数据库，
    一次依赖抖动会触发编排系统重启全部实例，把「依赖故障」放大成
    「服务整体不可用」；依赖的正确性由 ``/ready`` 判断。
    """
    return HealthResponse(status="ok", uptime_seconds=service.uptime_seconds)


@router.get(
    "/ready",
    response_model=ReadinessReport,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessReport, "description": "依赖未就绪"}},
)
async def ready(service: HealthService = Depends(get_health)) -> JSONResponse:
    """就绪探测：数据库可访问且默认模型配置自洽时返回 200，否则 503。

    WHY 用状态码表达结果而不把它塞进 200 的响应体：探活系统（k8s probe、
    ELB health check）只看状态码，把判定藏在 body 里等于没有判定。
    """
    report = await service.readiness()
    http_status = (
        status.HTTP_200_OK if report.ready else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    if not report.ready:
        logger.warning("就绪探测返回 503：%s", report.model_dump())
    return JSONResponse(status_code=http_status, content=report.model_dump())


@router.get("/metrics", response_model=MetricsSnapshot)
async def metrics(service: HealthService = Depends(get_health)) -> MetricsSnapshot:
    """运行指标快照。

    WHY 用 JSON 而不是 Prometheus 文本格式：指标只有四个计数，引入 exposition
    格式的解析与注册中心属于过度设计；等真正接入 Prometheus 时，在此处加一个
    内容协商分支即可，不需要改动服务层。
    """
    return await service.metrics()


__all__ = ["router"]
