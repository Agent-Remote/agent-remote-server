import asyncio
import os
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis

from agent_remote_server.device_relay_revocation import DeviceRelayRevocationBus


def test_redis_relay_revocation_reaches_another_worker() -> None:
    """在真实 Redis 中验证撤销事件能到达另一 worker。"""

    redis_url = os.getenv("AGENT_REMOTE_INTEGRATION_REDIS_URL")
    if redis_url is None:
        pytest.skip("AGENT_REMOTE_INTEGRATION_REDIS_URL is not configured")

    async def run() -> None:
        publisher = Redis.from_url(redis_url, decode_responses=True)
        subscriber = Redis.from_url(redis_url, decode_responses=True)
        bus = DeviceRelayRevocationBus(
            publisher,
            subscriber,
            channel=f"agent-remote:test-revocation:{uuid4()}",
        )
        received: asyncio.Future[tuple[str, int]] = asyncio.get_running_loop().create_future()

        async def handle(device_session_id: UUID, generation: int) -> None:
            if not received.done():
                received.set_result((str(device_session_id), generation))

        try:
            await bus.start(handle)
            device_session_id = uuid4()
            await asyncio.sleep(0.05)
            await bus.publish(device_session_id, 7)
            assert await asyncio.wait_for(received, timeout=2) == (str(device_session_id), 7)
        finally:
            await bus.close()

    asyncio.run(run())
