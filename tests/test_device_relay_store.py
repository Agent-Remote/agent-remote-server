import asyncio
import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from agent_remote_server.device_relay_store import (
    DeviceRelayBinding,
    DeviceRelayTicketClaims,
    InMemoryDeviceRelayStore,
    RedisDeviceRelayStore,
)


async def test_in_memory_relay_store_exchanges_each_role_once_and_consumes_atomically() -> None:
    """验证内存实现的角色交换和票据消费具有一次性语义。"""

    store = InMemoryDeviceRelayStore()
    binding = relay_binding()
    device = await store.exchange(
        binding=binding,
        role="device",
        spki_sha256="1" * 64,
        exporter_context="candidate-device",
        ttl=30,
    )
    assert device.status == "waiting"
    proxy = await store.exchange(
        binding=binding,
        role="proxy",
        spki_sha256="2" * 64,
        exporter_context="candidate-proxy",
        ttl=30,
    )
    assert proxy.status == "ready"
    assert proxy.peer_spki_sha256 == "1" * 64
    device = await store.exchange(
        binding=binding,
        role="device",
        spki_sha256="1" * 64,
        exporter_context="ignored-candidate",
        ttl=30,
    )
    assert device.status == "ready"
    assert device.peer_spki_sha256 == "2" * 64
    assert device.exporter_context == proxy.exporter_context
    repeated = await store.exchange(
        binding=binding,
        role="device",
        spki_sha256="1" * 64,
        exporter_context="ignored-candidate",
        ttl=30,
    )
    assert repeated.status == "already_issued"

    claims = DeviceRelayTicketClaims(
        binding=binding,
        role="device",
        credential_id=uuid4(),
    )
    await store.issue_ticket(token_hash="ticket-hash", claims=claims, ttl=30)
    winners = await asyncio.gather(
        *(store.consume_ticket(token_hash="ticket-hash") for _ in range(16))
    )
    assert winners.count(claims) == 1
    assert winners.count(None) == 15


def test_redis_relay_store_preserves_atomic_one_time_semantics() -> None:
    """在真实 Redis 中验证 Lua 交换和 GETDEL 票据只能各成功一次。"""

    redis_url = integration_url("AGENT_REMOTE_INTEGRATION_REDIS_URL")
    if redis_url is None:
        pytest.skip("AGENT_REMOTE_INTEGRATION_REDIS_URL is not configured")

    async def run() -> None:
        redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        await redis.flushdb()
        store = RedisDeviceRelayStore(redis)
        try:
            binding = relay_binding()
            waiting = await store.exchange(
                binding=binding,
                role="device",
                spki_sha256="a" * 64,
                exporter_context="first-candidate",
                ttl=30,
            )
            assert waiting.status == "waiting"
            proxy = await store.exchange(
                binding=binding,
                role="proxy",
                spki_sha256="b" * 64,
                exporter_context="shared-context",
                ttl=30,
            )
            assert proxy.status == "ready"
            device = await store.exchange(
                binding=binding,
                role="device",
                spki_sha256="a" * 64,
                exporter_context="ignored-candidate",
                ttl=30,
            )
            assert device.status == "ready"
            assert device.exporter_context == proxy.exporter_context

            claims = DeviceRelayTicketClaims(
                binding=binding,
                role="proxy",
                credential_id=None,
            )
            await store.issue_ticket(token_hash="redis-ticket", claims=claims, ttl=30)
            winners = await asyncio.gather(
                *(store.consume_ticket(token_hash="redis-ticket") for _ in range(16))
            )
            assert winners.count(claims) == 1
            assert winners.count(None) == 15
        finally:
            await store.close()

    asyncio.run(run())


def relay_binding() -> DeviceRelayBinding:
    """
    创建测试使用的完整设备中继绑定

    :return DeviceRelayBinding: 测试设备中继绑定
    """

    return DeviceRelayBinding(
        user_id=uuid4(),
        device_id=uuid4(),
        tool_session_id=uuid4(),
        device_session_id=uuid4(),
        node_id=uuid4(),
        generation=1,
    )


def integration_url(name: str) -> str | None:
    """
    读取显式启用的真实依赖集成测试地址

    :param name (str): 环境变量名称

    :return str | None: 集成测试地址
    """

    return os.getenv(name)
