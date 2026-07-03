import asyncio
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request

from agent_remote_server import __version__
from agent_remote_server.api.deps import get_settings
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.db import check_database
from agent_remote_server.redis_client import check_redis
from agent_remote_server.schemas.health import HealthComponent, HealthResponse

router = APIRouter(tags=["health"])


def _base_response(
    *,
    request: Request,
    settings: Settings,
    status: Literal["ok", "degraded"],
    components: dict[str, HealthComponent],
) -> HealthResponse:
    return HealthResponse(
        status=status,
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        request_id=get_request_id() or request.headers.get(settings.request_id_header),
        components=components,
    )


@router.get("/healthz", response_model=HealthResponse)
async def healthz(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    """
    返回进程级健康状态

    :param request (Request): 当前请求对象
    :param settings (Settings): 应用配置

    :return HealthResponse: 健康检查响应
    """

    started_at = time.perf_counter()
    component = HealthComponent(
        status="ok",
        latency_ms=round((time.perf_counter() - started_at) * 1000, 3),
    )
    return _base_response(
        request=request,
        settings=settings,
        status="ok",
        components={"process": component},
    )


@router.get("/readyz", response_model=HealthResponse)
async def readyz(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    """
    返回依赖服务就绪状态

    :param request (Request): 当前请求对象
    :param settings (Settings): 应用配置

    :return HealthResponse: 就绪检查响应
    """

    database, redis = await asyncio.gather(check_database(settings), check_redis(settings))
    components = {"database": database, "redis": redis}
    status: Literal["ok", "degraded"] = (
        "ok" if all(item.status == "ok" for item in components.values()) else "degraded"
    )
    return _base_response(
        request=request,
        settings=settings,
        status=status,
        components=components,
    )
