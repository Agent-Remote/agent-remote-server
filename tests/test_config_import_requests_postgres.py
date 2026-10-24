"""
在显式启用的独立 PostgreSQL 数据库验证真实迁移、并发互斥和保留凭据的降级保护。
"""

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from config_import_request_support import endpoint, request_payload, setup_import
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_tool_accounts_api import auth_header

from agent_remote_server.config import Settings, get_settings
from agent_remote_server.main import create_app
from agent_remote_server.models import AuditLog, NodeTask, ToolAccountConfigImportRequest
from agent_remote_server.repositories.config_import_requests import ConfigImportRequestRepository

pytestmark = pytest.mark.xdist_group("external_services")


async def postgres_counts(url: str) -> tuple[int, int, int]:
    """
    使用新连接确认三个原子记录均只发布一次。

    :param url (str): 显式隔离测试数据库 URL
    :return tuple[int, int, int]: 原始凭据、原任务及导入审计数量
    """
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as session:
            receipts = await session.scalar(
                select(func.count()).select_from(ToolAccountConfigImportRequest)
            )
            tasks = await session.scalar(
                select(func.count())
                .select_from(NodeTask)
                .where(NodeTask.task_type == "import_tool_account_config")
            )
            audits = await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "tool_accounts.config_import_start")
            )
            return int(receipts or 0), int(tasks or 0), int(audits or 0)
    finally:
        await engine.dispose()


def test_postgres_atomic_caller_key_and_guarded_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    两个真正独立事务同时看到空键，也只能发布一个接受且不能降级抹除它。

    :param monkeypatch (pytest.MonkeyPatch): 隔离迁移配置及确定性并发边界
    """
    raw_url = os.environ.get("AGENT_REMOTE_CONFIG_IMPORT_TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("explicit disposable config-import PostgreSQL URL is not configured")
    parsed = make_url(raw_url)
    if (
        parsed.host != "127.0.0.1"
        or parsed.database != "config_import_fixture"
        or parsed.username != "config_import_fixture"
    ):
        pytest.fail("refusing a database outside the dedicated local config-import fixture")
    monkeypatch.setenv("DATABASE_URL", raw_url)
    get_settings.cache_clear()
    migration = Config()
    migration.set_main_option("script_location", "migrations")
    try:
        command.upgrade(migration, "head")
        settings = Settings(secret_key="test-secret", log_level="CRITICAL", database_url=raw_url)
        with TestClient(create_app(settings)) as client:
            token, account = setup_import(client)
            payload = request_payload(account)
            start = threading.Barrier(2, timeout=10)
            initial_reads = asyncio.Barrier(2)
            original = ConfigImportRequestRepository.request
            calls = 0

            async def synchronized_read(
                self: ConfigImportRequestRepository, user_id: UUID, request_id: UUID
            ) -> ToolAccountConfigImportRequest | None:
                """
                保证两事务在加锁前确实都观察到尚未接受。

                :param user_id (UUID): 当前认证用户
                :param request_id (UUID): 相同原始调用方键
                :return ToolAccountConfigImportRequest | None: 原始数据库观察
                """
                nonlocal calls
                record = await original(self, user_id, request_id)
                calls += 1
                if calls <= 2:
                    assert record is None
                    await asyncio.wait_for(initial_reads.wait(), timeout=10)
                return record

            monkeypatch.setattr(ConfigImportRequestRepository, "request", synchronized_read)

            def submit() -> Response:
                """
                从两个线程派发真正独立的请求事务。

                :return Response: 首次接受或原始凭据重放
                """
                start.wait()
                return client.post(endpoint(account), headers=auth_header(token), json=payload)

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(submit), pool.submit(submit)]
                responses = [future.result(timeout=30) for future in futures]
            assert sorted(response.status_code for response in responses) == [200, 201]
            assert responses[0].json()["data"] == responses[1].json()["data"]
            recovered = client.get(
                endpoint(account) + f"/{payload['request_id']}", headers=auth_header(token)
            )
            assert recovered.status_code == 200
            assert recovered.json()["data"] == responses[0].json()["data"]
        assert asyncio.run(postgres_counts(raw_url)) == (1, 1, 1)
        with pytest.raises(RuntimeError, match="cannot downgrade accepted config import requests"):
            command.downgrade(migration, "-1")
        assert asyncio.run(postgres_counts(raw_url)) == (1, 1, 1)
    finally:
        get_settings.cache_clear()
