"""
验证真实数据库事务不会阻塞中继配对或保留过期准入状态。
"""

import asyncio
import os
from typing import cast

import pytest
from fastapi import FastAPI, WebSocket
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ego_browser_concurrency import _seed_active_binding
from test_ego_browser_relay import _FakeWebSocket, _outer, _raw

from agent_remote_server.api.ego_browser import ego_browser_relay
from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.ego_browser.relay import (
    EgoBrowserRelayBinding,
    EgoBrowserRelayHub,
    EgoBrowserRelayRole,
    EgoBrowserRelayTicketClaims,
    InMemoryEgoBrowserRelayStore,
)
from agent_remote_server.models import EgoBrowserBinding, EgoBrowserDevice
from agent_remote_server.security import hash_token


class _RouteSocket(_FakeWebSocket):
    """
    向真实中继路由提供应用状态与认证头。
    """

    def __init__(self, app: FastAPI, token: str) -> None:
        """
        初始化隔离连接。

        :param app (FastAPI): 测试应用
        :param token (str): 测试票据
        """
        super().__init__()
        self.app = app
        self.headers = {"authorization": f"Bearer {token}"}


@pytest.mark.parametrize("transport", ["memory", "redis"])
def test_postgres_relay_pairs_without_retaining_device_lock(transport: str) -> None:
    """
    双端通过真实路由配对，空闲时释放行锁，每帧重新读取绑定状态。

    :param transport (str): 中继传输后端
    """
    database_url = os.getenv("AGENT_REMOTE_INTEGRATION_DATABASE_URL")
    redis_url = os.getenv("AGENT_REMOTE_INTEGRATION_REDIS_URL")
    if database_url is None or (transport == "redis" and redis_url is None):
        pytest.skip("isolated PostgreSQL and Redis integration URLs are required")

    async def scenario() -> None:
        """
        执行双端配对和数据库锁回归场景。
        """
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        redis = Redis.from_url(redis_url) if transport == "redis" and redis_url else None
        hub = EgoBrowserRelayHub(
            maximum_frame_bytes=16_384,
            pair_timeout_seconds=2,
            maximum_bytes_per_second=1_000_000,
            maximum_connection_seconds=10,
            redis=redis,
        )
        tasks: list[asyncio.Task[None]] = []
        settings = Settings(secret_key="relay-database-test", ego_browser_bridge_enabled=True)
        app = FastAPI()
        store = InMemoryEgoBrowserRelayStore()
        app.state.settings = settings
        app.state.session_factory = factory
        app.state.ego_browser_relay_store = store
        app.state.ego_browser_relay_hub = hub
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                user, node, device, binding = await _seed_active_binding(session)
                device.server_origin = settings.public_origin
                await session.commit()
                identity = EgoBrowserRelayBinding(
                    user_id=user.id,
                    ego_browser_device_id=device.id,
                    tool_session_id=binding.binding_tool_session_id,
                    binding_id=binding.id,
                    node_id=node.id,
                    generation=1,
                )

            async def open_endpoint(role: EgoBrowserRelayRole) -> _RouteSocket:
                """
                签发单次测试票据并启动真实路由。

                :param role (EgoBrowserRelayRole): 中继角色
                :return _RouteSocket: 测试连接
                """
                token = f"test-ticket-{role}"
                await store.issue_ticket(
                    token_hash=hash_token(settings.secret_key, token),
                    claims=EgoBrowserRelayTicketClaims(binding=identity, role=role),
                    ttl=60,
                )
                socket = _RouteSocket(app, token)
                tasks.append(
                    asyncio.create_task(ego_browser_relay(binding.id, cast(WebSocket, socket)))
                )
                async with asyncio.timeout(3):
                    while not socket.accepted:
                        await asyncio.sleep(0.01)
                return socket

            bridge = await open_endpoint("bridge")
            async with factory() as session:
                assert (
                    await session.scalar(
                        select(EgoBrowserDevice)
                        .where(EgoBrowserDevice.id == device.id)
                        .with_for_update(nowait=True)
                    )
                    is not None
                )
            wrapper = await open_endpoint("wrapper")
            request = _raw(_outer(binding_id=str(binding.id)))
            await wrapper.messages.put({"type": "websocket.receive", "bytes": request})
            async with asyncio.timeout(3):
                while not bridge.sent:
                    await asyncio.sleep(0.01)
            assert bridge.sent == [request]
            response = _raw(_outer(binding_id=str(binding.id), direction="response"))
            await bridge.messages.put({"type": "websocket.receive", "bytes": response})
            async with asyncio.timeout(3):
                while not wrapper.sent:
                    await asyncio.sleep(0.01)
            assert wrapper.sent == [response]

            async with factory() as session:
                current = await session.scalar(
                    select(EgoBrowserBinding)
                    .where(EgoBrowserBinding.id == binding.id)
                    .with_for_update(nowait=True)
                )
                assert current is not None
                current.status = "stopped"
                await session.commit()
            next_request = {
                **_outer(binding_id=str(binding.id)),
                "sequence": 2,
                "request_id": "next",
            }
            await wrapper.messages.put({"type": "websocket.receive", "bytes": _raw(next_request)})
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
            assert 1008 in wrapper.close_codes
            assert bridge.sent == [request]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await hub.close()
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.drop_all)
            await engine.dispose()

    asyncio.run(scenario())
