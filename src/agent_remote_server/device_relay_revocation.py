"""设备 relay 跨 worker 撤销通知。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis

from agent_remote_server.config import Settings

RevocationHandler = Callable[[UUID, int], Awaitable[None]]
logger = logging.getLogger(__name__)


class DeviceRelayRevocationPublisher(Protocol):
    """relay hub 所需的最小跨 worker 发布接口。"""

    async def publish(self, device_session_id: UUID, generation: int) -> None:
        """
        发布一个 generation 撤销事件

        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 被撤销的连接代次
        """


class DeviceRelayRevocationBus:
    """通过 Redis pub/sub 广播已提交的 device-session generation 撤销。"""

    def __init__(self, redis: Redis, subscriber: Redis, *, channel: str) -> None:
        self._redis = redis
        self._subscriber = subscriber
        self._channel = channel
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self, handler: RevocationHandler) -> None:
        """
        启动后台订阅任务

        :param handler (RevocationHandler): 收到撤销事件时调用的异步回调
        """

        if self._task is not None:
            return
        # Do not advertise device control until both Redis connections are usable.
        # The subscriber task can reconnect later, but a failed initial check must
        # fail application startup rather than silently allowing cross-worker drift.
        await self._redis.ping()
        await self._subscriber.ping()
        self._task = asyncio.create_task(self._run(handler))

    async def publish(self, device_session_id: UUID, generation: int) -> None:
        """
        发布一个不含敏感材料的撤销事件

        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 被撤销的连接代次
        """

        await self._redis.publish(
            self._channel,
            json.dumps(
                {
                    "device_session_id": str(device_session_id),
                    "generation": generation,
                },
                separators=(",", ":"),
            ),
        )

    async def close(self) -> None:
        """停止订阅并关闭 Redis 连接。"""

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
                    if message is None:
                        continue
                    if message.get("type") != "message":
                        continue
                    try:
                        payload = json.loads(message["data"])
                        device_session_id = UUID(payload["device_session_id"])
                        generation = int(payload["generation"])
                        if generation < 1:
                            continue
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    await handler(device_session_id, generation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "device relay revocation subscriber reconnecting",
                    extra={"error_type": type(exc).__name__},
                )
                await asyncio.sleep(1)
            finally:
                await asyncio.gather(
                    pubsub.unsubscribe(self._channel),
                    pubsub.aclose(),
                    return_exceptions=True,
                )


class NoopDeviceRelayRevocationBus:
    """SQLite 测试和单进程测试使用的无外部依赖通知实现。"""

    async def start(self, handler: RevocationHandler) -> None:
        """
        兼容生产 bus 的启动接口

        :param handler (RevocationHandler): 收到撤销事件时调用的异步回调
        """

    async def publish(self, device_session_id: UUID, generation: int) -> None:
        """
        忽略测试环境中的跨进程通知

        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 被撤销的连接代次
        """

    async def close(self) -> None:
        """兼容生产 bus 的关闭接口。"""


def create_device_relay_revocation_bus(
    settings: Settings,
) -> DeviceRelayRevocationBus | NoopDeviceRelayRevocationBus:
    """
    按照部署数据库类型创建 relay 撤销通知总线

    :param settings (Settings): 应用配置

    :return DeviceRelayRevocationBus | NoopDeviceRelayRevocationBus: 按部署数据库类型创建的撤销总线
    """

    if settings.database_url.startswith("sqlite"):
        return NoopDeviceRelayRevocationBus()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    subscriber = Redis.from_url(settings.redis_url, decode_responses=True)
    return DeviceRelayRevocationBus(
        redis,
        subscriber,
        channel="agent-remote:device-relay-revocation",
    )
