import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent_remote_server import device_control_retention
from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.models import AuditLog, DeviceSession, DeviceSessionApproval
from agent_remote_server.services.device_control_retention import DeviceControlRetentionService


async def test_retention_cleanup_deletes_only_expired_terminal_device_metadata() -> None:
    """保留清理必须保留活动、近期和非设备会话审计数据。"""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    now = datetime(2026, 7, 31, 4, 0, tzinfo=UTC)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            expired_terminal = DeviceSession(
                id=uuid4(),
                user_id=uuid4(),
                device_id=uuid4(),
                tool_session_id=uuid4(),
                node_id=uuid4(),
                platform="macos",
                status="stopped",
                generation=2,
                expires_at=now - timedelta(days=11),
                stopped_at=now - timedelta(days=11),
            )
            recent_terminal = DeviceSession(
                id=uuid4(),
                user_id=uuid4(),
                device_id=uuid4(),
                tool_session_id=uuid4(),
                node_id=uuid4(),
                platform="macos",
                status="failed",
                generation=2,
                expires_at=now - timedelta(days=5),
                stopped_at=now - timedelta(days=5),
            )
            active = DeviceSession(
                id=uuid4(),
                user_id=uuid4(),
                device_id=uuid4(),
                tool_session_id=uuid4(),
                node_id=uuid4(),
                platform="macos",
                status="active",
                generation=1,
                expires_at=now + timedelta(hours=1),
                stopped_at=now - timedelta(days=30),
            )
            session.add_all([expired_terminal, recent_terminal, active])
            session.add_all(
                [
                    DeviceSessionApproval(
                        device_session_id=expired_terminal.id,
                        application_digest="a" * 64,
                        control_level="view_only",
                        approval_result="allowed",
                        clipboard_allowed=False,
                        audit_correlation_id=uuid4(),
                    ),
                    DeviceSessionApproval(
                        device_session_id=recent_terminal.id,
                        application_digest="b" * 64,
                        control_level="view_only",
                        approval_result="allowed",
                        clipboard_allowed=False,
                        audit_correlation_id=uuid4(),
                    ),
                ]
            )
            old_device_audit = AuditLog(
                action="device_session.stop",
                target_type="device_session",
                target_id=str(expired_terminal.id),
                details={},
                created_at=now - timedelta(days=21),
            )
            recent_device_audit = AuditLog(
                action="device_session.stop",
                target_type="device_session",
                target_id=str(recent_terminal.id),
                details={},
                created_at=now - timedelta(days=5),
            )
            old_identity_audit = AuditLog(
                action="devices.revoke",
                target_type="user_device",
                target_id=str(active.device_id),
                details={},
                created_at=now - timedelta(days=30),
            )
            session.add_all([old_device_audit, recent_device_audit, old_identity_audit])
            await session.commit()

            result = await DeviceControlRetentionService(
                session,
                Settings(
                    device_session_retention_days=10,
                    device_session_audit_retention_days=20,
                    device_control_retention_cleanup_batch_size=100,
                ),
            ).cleanup(now=now)

            assert result.sessions_deleted == 1
            assert result.audits_deleted == 1
            assert await session.get(DeviceSession, expired_terminal.id) is None
            assert await session.get(DeviceSession, recent_terminal.id) is not None
            assert await session.get(DeviceSession, active.id) is not None
            approvals = list(await session.scalars(select(DeviceSessionApproval)))
            assert [approval.device_session_id for approval in approvals] == [recent_terminal.id]
            audit_logs = list(await session.scalars(select(AuditLog)))
            assert {audit_log.id for audit_log in audit_logs} == {
                recent_device_audit.id,
                old_identity_audit.id,
            }
    finally:
        await engine.dispose()


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def test_retention_runner_executes_cleanup_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台保留任务必须执行一轮清理并及时响应关闭。"""

    stop = asyncio.Event()
    calls = 0

    class Service:
        def __init__(self, *_args: object) -> None:
            pass

        async def cleanup(self) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            stop.set()
            return SimpleNamespace(changed=2, sessions_deleted=1, audits_deleted=1)

    monkeypatch.setattr(device_control_retention, "DeviceControlRetentionService", Service)
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        device_session_retention_days=10,
        device_session_audit_retention_days=20,
        device_control_retention_cleanup_interval_seconds=0.001,
    )
    app.state.session_factory = _Session

    asyncio.run(device_control_retention.run_device_control_retention(app, stop))

    assert calls == 1
