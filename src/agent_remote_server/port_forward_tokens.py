import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis

from agent_remote_server.config import Settings


@dataclass(frozen=True)
class PortForwardTokenClaims:
    """
    一次性端口转发 token 声明
    """

    forward_id: UUID
    device_id: UUID
    ssh_key_id: UUID | None


class PortForwardTokenStore(Protocol):
    """
    一次性端口转发 token 存储协议
    """

    async def issue(self, *, token_hash: str, claims: PortForwardTokenClaims, ttl: int) -> None:
        """写入一次性 token 声明。"""

    async def consume(self, *, token_hash: str) -> PortForwardTokenClaims | None:
        """原子消费一次性 token 声明。"""

    async def allow(self, *, scope: str, limit: int, window_seconds: int) -> bool:
        """原子增加限速计数并返回当前请求是否允许。"""

    async def close(self) -> None:
        """关闭 token store 连接。"""


class RedisPortForwardTokenStore:
    """
    Redis 一次性端口转发 token 存储
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def issue(self, *, token_hash: str, claims: PortForwardTokenClaims, ttl: int) -> None:
        """写入一次性 token 声明。"""

        payload = json.dumps(
            {
                key: str(value) if value is not None else None
                for key, value in asdict(claims).items()
            }
        )
        created = await self._redis.set(self._key(token_hash), payload, ex=ttl, nx=True)
        if not created:
            raise RuntimeError("port forward token collision")

    async def consume(self, *, token_hash: str) -> PortForwardTokenClaims | None:
        """原子消费一次性 token 声明。"""

        payload = await self._redis.getdel(self._key(token_hash))
        if payload is None:
            return None
        values = json.loads(payload)
        return PortForwardTokenClaims(
            forward_id=UUID(values["forward_id"]),
            device_id=UUID(values["device_id"]),
            ssh_key_id=UUID(values["ssh_key_id"]) if values.get("ssh_key_id") else None,
        )

    async def allow(self, *, scope: str, limit: int, window_seconds: int) -> bool:
        """原子增加限速计数并返回当前请求是否允许。"""

        key = f"agent-remote:port-forward-rate:{scope}"
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.incr(key)
            pipeline.expire(key, window_seconds, nx=True)
            values = await pipeline.execute()
        return int(values[0]) <= limit

    async def close(self) -> None:
        """关闭 Redis 连接。"""

        await self._redis.aclose()

    def _key(self, token_hash: str) -> str:
        return f"agent-remote:port-forward-token:{token_hash}"


class InMemoryPortForwardTokenStore:
    """
    测试使用的一次性端口转发 token 存储
    """

    def __init__(self) -> None:
        self._values: dict[str, tuple[PortForwardTokenClaims, datetime]] = {}
        self._rates: dict[str, tuple[int, datetime]] = {}
        self._lock = asyncio.Lock()

    async def issue(self, *, token_hash: str, claims: PortForwardTokenClaims, ttl: int) -> None:
        """写入一次性 token 声明。"""

        async with self._lock:
            if token_hash in self._values:
                raise RuntimeError("port forward token collision")
            self._values[token_hash] = (claims, datetime.now(UTC) + timedelta(seconds=ttl))

    async def consume(self, *, token_hash: str) -> PortForwardTokenClaims | None:
        """原子消费一次性 token 声明。"""

        async with self._lock:
            value = self._values.pop(token_hash, None)
        if value is None or value[1] <= datetime.now(UTC):
            return None
        return value[0]

    async def allow(self, *, scope: str, limit: int, window_seconds: int) -> bool:
        """原子增加限速计数并返回当前请求是否允许。"""

        now = datetime.now(UTC)
        async with self._lock:
            count, expires_at = self._rates.get(scope, (0, now))
            if expires_at <= now:
                count = 0
                expires_at = now + timedelta(seconds=window_seconds)
            count += 1
            self._rates[scope] = (count, expires_at)
            return count <= limit

    async def close(self) -> None:
        """清理内存 token。"""

        async with self._lock:
            self._values.clear()
            self._rates.clear()


def create_port_forward_token_store(settings: Settings) -> RedisPortForwardTokenStore:
    """
    创建生产 Redis token store

    :param settings (Settings): 应用配置

    :return RedisPortForwardTokenStore: Redis 令牌存储
    """

    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return RedisPortForwardTokenStore(redis)
