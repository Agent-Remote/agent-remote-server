import asyncio
import time

from redis.asyncio import Redis

from agent_remote_server.config import Settings
from agent_remote_server.schemas.health import HealthComponent


async def check_redis(settings: Settings) -> HealthComponent:
    """
    检查 Redis 可用性

    :param settings (Settings): 应用配置

    :return HealthComponent: Redis 健康状态
    """

    started_at = time.perf_counter()
    client: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await asyncio.wait_for(
            client.ping(),
            timeout=settings.dependency_check_timeout_seconds,
        )
    except Exception as exc:
        return HealthComponent(
            status="error",
            latency_ms=round((time.perf_counter() - started_at) * 1000, 3),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        await client.aclose()

    return HealthComponent(
        status="ok",
        latency_ms=round((time.perf_counter() - started_at) * 1000, 3),
    )
