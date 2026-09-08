"""广播 ego-browser binding generation 的撤销事件。"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from redis.asyncio import Redis

from agent_remote_server.config import Settings
from agent_remote_server.ego_browser.relay.contracts import RevocationHandler

logger = logging.getLogger("agent_remote_server.ego_browser.relay")

_DISTRIBUTED_REVOCATION_TTL_SECONDS = 1200


def _distributed_revocation_key(binding_id: UUID, generation: int) -> str:
    return f"agent-remote:ego-browser:revoked:{binding_id}:{generation}"


class EgoBrowserRevocationBus:
    """通过独立 Redis 频道广播 ego-browser 代次撤销。"""

    def __init__(self, redis: Redis, subscriber: Redis, *, channel: str) -> None:
        self._redis = redis
        self._subscriber = subscriber
        self._channel = channel
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self, handler: RevocationHandler) -> None:
        """
        启动 Redis 订阅任务并校验两个连接可用。

        :param handler (RevocationHandler): 收到撤销事件时调用的异步处理器
        """

        if self._task is not None:
            return
        await self._redis.ping()
        await self._subscriber.ping()
        self._task = asyncio.create_task(self._run(handler))

    async def publish(self, binding_id: UUID, generation: int) -> None:
        """
        发布 binding ID 和旧 generation，不携带脚本或连接材料。

        :param binding_id (UUID): ego-browser binding 标识
        :param generation (int): 目标 binding generation
        """

        await self._redis.set(
            _distributed_revocation_key(binding_id, generation),
            "1",
            ex=_DISTRIBUTED_REVOCATION_TTL_SECONDS,
        )
        await self._redis.publish(
            self._channel,
            json.dumps(
                {"binding_id": str(binding_id), "generation": generation},
                separators=(",", ":"),
            ),
        )

    async def close(self) -> None:
        """停止订阅任务并关闭 Redis 连接。"""

        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._subscriber.aclose()
        await self._redis.aclose()

    async def _run(self, handler: RevocationHandler) -> None:
        while not self._stop.is_set():
            pubsub = self._subscriber.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe(self._channel)
                while not self._stop.is_set():
                    message = await pubsub.get_message(timeout=1.0)
                    if message is None or message.get("type") != "message":
                        continue
                    try:
                        payload = json.loads(message["data"])
                        binding_id = UUID(str(payload["binding_id"]))
                        generation = int(payload["generation"])
                        if generation < 1:
                            continue
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    await handler(binding_id, generation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "ego browser relay revocation subscriber reconnecting",
                    extra={"error_type": type(exc).__name__},
                )
                await asyncio.sleep(1)
            finally:
                await asyncio.gather(
                    pubsub.unsubscribe(self._channel),
                    pubsub.aclose(),
                    return_exceptions=True,
                )


class InMemoryEgoBrowserRevocationBus:
    """SQLite/单进程使用的本地撤销通知实现。"""

    def __init__(self) -> None:
        self._handler: RevocationHandler | None = None

    async def start(self, handler: RevocationHandler) -> None:
        """
        保存本地撤销处理器。

        :param handler (RevocationHandler): 收到撤销事件时调用的异步处理器
        """

        self._handler = handler

    async def publish(self, binding_id: UUID, generation: int) -> None:
        """
        立即通知本进程 relay hub。

        :param binding_id (UUID): ego-browser binding 标识
        :param generation (int): 目标 binding generation
        """

        if self._handler is not None:
            await self._handler(binding_id, generation)

    async def close(self) -> None:
        """清理本地撤销处理器。"""

        self._handler = None


def create_ego_browser_revocation_bus(
    settings: Settings,
) -> EgoBrowserRevocationBus | InMemoryEgoBrowserRevocationBus:
    """
    按部署数据库类型创建独立撤销总线。

    :param settings (Settings): 应用配置

    :return EgoBrowserRevocationBus | InMemoryEgoBrowserRevocationBus: 按当前部署模式创建的撤销总线
    """

    if settings.database_url.startswith("sqlite"):
        return InMemoryEgoBrowserRevocationBus()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    subscriber = Redis.from_url(settings.redis_url, decode_responses=True)
    return EgoBrowserRevocationBus(
        redis,
        subscriber,
        channel="agent-remote:ego-browser-revocation",
    )
