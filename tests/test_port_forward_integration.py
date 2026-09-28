"""
验证端口转发集成行为。
"""

import asyncio
import logging
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from agent_remote_server.config import get_settings
from agent_remote_server.port_forwarding.tokens import (
    PortForwardTokenClaims,
    RedisPortForwardTokenStore,
)


def integration_url(name: str) -> str:
    """
    读取显式启用的真实依赖集成测试地址。

    :param name (str): 名称
    :return str: 集成 URL
    """

    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} is not configured")
    return value


def test_postgres_migration_creates_port_forward_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    从空 PostgreSQL 数据库执行完整 migration 并核对关键账本字段。

    :param monkeypatch (pytest.MonkeyPatch): pytest 补丁工具
    """

    database_url = integration_url("AGENT_REMOTE_INTEGRATION_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    root_handlers = logging.getLogger().handlers[:]
    service_logger = logging.getLogger("agent_remote_server.services.ego_browser")
    logger_disabled = service_logger.disabled
    try:
        # 集成迁移不加载 CLI 日志配置，避免禁用同进程业务日志及 pytest 捕获器。
        migration_config = Config()
        migration_config.set_main_option("script_location", "migrations")
        command.upgrade(migration_config, "head")
    finally:
        get_settings.cache_clear()
    assert logging.getLogger().handlers == root_handlers
    assert service_logger.disabled == logger_disabled

    async def inspect_schema() -> None:
        """
        检查模型。
        """
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                columns = await connection.run_sync(
                    lambda sync_connection: {
                        value["name"]
                        for value in inspect(sync_connection).get_columns("port_forwards")
                    }
                )
                assert {
                    "ssh_key_id",
                    "connection_generation",
                    "generation_bytes_up",
                    "generation_bytes_down",
                    "generation_connection_count",
                    "lease_expires_at",
                    "expires_at",
                } <= columns
        finally:
            await engine.dispose()

    asyncio.run(inspect_schema())


def test_redis_connection_token_has_single_atomic_winner() -> None:
    """
    真实 Redis 并发消费一次性 token 时只能有一个成功者。
    """

    redis_url = integration_url("AGENT_REMOTE_INTEGRATION_REDIS_URL")

    async def consume_once() -> None:
        """
        消费一次。
        """
        redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        store = RedisPortForwardTokenStore(redis)
        token_hash = f"integration-{uuid4()}"
        claims = PortForwardTokenClaims(
            forward_id=uuid4(),
            device_id=uuid4(),
            ssh_key_id=None,
        )
        try:
            await store.issue(token_hash=token_hash, claims=claims, ttl=30)
            results = await asyncio.gather(
                *(store.consume(token_hash=token_hash) for _ in range(16))
            )
            winners = [result for result in results if result is not None]
            assert winners == [claims]
            allowed = await asyncio.gather(
                *(
                    store.allow(scope=f"integration-{token_hash}", limit=3, window_seconds=30)
                    for _ in range(8)
                )
            )
            assert sum(allowed) == 3
        finally:
            await store.close()

    asyncio.run(consume_once())
