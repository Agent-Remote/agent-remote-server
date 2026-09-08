import asyncio
import base64
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.ego_browser.relay import (
    EgoBrowserRelayBinding,
    EgoBrowserRelayTicketClaims,
)
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    EgoBrowserBinding,
    EgoBrowserDevice,
    EgoBrowserRequestLedger,
    EgoBrowserRevocationOutbox,
    Node,
    User,
)
from agent_remote_server.schemas.ego_browser import EgoBrowserRenewRequest
from agent_remote_server.services.ego_browser import REQUIRED_CAPABILITIES, EgoBrowserService


@pytest.mark.parametrize("first_operation", ["renew", "stop"])
def test_stop_wins_concurrent_postgres_renewal(
    first_operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实行锁的两种取得顺序最终都必须保留停止状态。"""

    database_url = os.getenv("AGENT_REMOTE_INTEGRATION_DATABASE_URL")
    if database_url is None:
        pytest.skip("AGENT_REMOTE_INTEGRATION_DATABASE_URL is not configured")

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as seed_session:
                user, _, device, binding = await _seed_active_binding(seed_session)
                binding_id = binding.id
                device_id = device.id
                user_id = user.id

            lock_acquired = asyncio.Event()
            release_lock = asyncio.Event()
            settings = Settings(
                ego_browser_bridge_enabled=True,
                ego_browser_require_device_pop=False,
            )
            async with session_factory() as renew_session, session_factory() as stop_session:
                renew_user = await renew_session.get(User, user_id)
                stop_user = await stop_session.get(User, user_id)
                assert renew_user is not None and stop_user is not None
                renew_service = EgoBrowserService(renew_session, settings)
                stop_service = EgoBrowserService(stop_session, settings)
                first_service = renew_service if first_operation == "renew" else stop_service
                original_get_binding = first_service._repository.get_binding  # noqa: SLF001

                async def get_binding_with_barrier(
                    current_binding_id: UUID,
                    *,
                    for_update: bool = False,
                ) -> EgoBrowserBinding | None:
                    current = await original_get_binding(
                        current_binding_id,
                        for_update=for_update,
                    )
                    if for_update:
                        lock_acquired.set()
                        await release_lock.wait()
                    return current

                monkeypatch.setattr(
                    first_service._repository,  # noqa: SLF001
                    "get_binding",
                    get_binding_with_barrier,
                )

                async def renew() -> EgoBrowserBinding:
                    return await renew_service.renew(
                        user=renew_user,
                        binding_id=binding_id,
                        device_id=device_id,
                        payload=EgoBrowserRenewRequest(
                            generation=1,
                            allowlist_revision=1,
                            learning_bundle_digest=None,
                        ),
                    )

                async def stop() -> EgoBrowserBinding:
                    return await stop_service.stop_by_user(
                        user=stop_user,
                        binding_id=binding_id,
                        generation=1,
                        reason="customer_secret_path",
                    )

                first = renew if first_operation == "renew" else stop
                second = stop if first_operation == "renew" else renew
                first_task = asyncio.create_task(first())
                await asyncio.wait_for(lock_acquired.wait(), timeout=2)
                second_task = asyncio.create_task(second())
                await asyncio.sleep(0.1)
                assert not second_task.done()
                release_lock.set()
                first_result, second_result = await asyncio.gather(
                    first_task,
                    second_task,
                    return_exceptions=True,
                )

                assert not isinstance(first_result, BaseException)
                if first_operation == "renew":
                    assert not isinstance(second_result, BaseException)
                else:
                    assert isinstance(second_result, ApiError)
                    assert second_result.code == "EGO_BROWSER_GENERATION_MISMATCH"

            async with session_factory() as assertion_session:
                durable = await assertion_session.get(EgoBrowserBinding, binding_id)
                assert durable is not None
                assert (durable.status, durable.generation) == ("stopped", 2)
                assert durable.lease_until is None
                assert durable.lease_health == "expired"
                assert durable.stop_reason == "other"
                outbox = list(
                    await assertion_session.scalars(
                        select(EgoBrowserRevocationOutbox).where(
                            EgoBrowserRevocationOutbox.binding_id == binding_id
                        )
                    )
                )
                assert [(item.generation, item.reason) for item in outbox] == [(1, "other")]
                actions = list(
                    await assertion_session.scalars(
                        select(AuditLog.action).where(AuditLog.target_id == str(binding_id))
                    )
                )
                assert actions.count("ego_browser_binding.stopped") == 1
                assert actions.count("ego_browser_binding.renewed") == (
                    1 if first_operation == "renew" else 0
                )
        finally:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.drop_all)
                await connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("first_operation", ["response", "cancel"])
def test_postgres_response_cancel_race_has_one_durable_request_outcome(
    first_operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Response 与 cancel 的真实行锁竞态必须由先取得 admission 的一方决定。"""

    database_url = os.getenv("AGENT_REMOTE_INTEGRATION_DATABASE_URL")
    if database_url is None:
        pytest.skip("AGENT_REMOTE_INTEGRATION_DATABASE_URL is not configured")

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as seed_session:
                user, node, device, binding = await _seed_active_binding(seed_session)
                seed_session.add(
                    EgoBrowserRequestLedger(
                        binding_id=binding.id,
                        generation=1,
                        request_id="request-response-cancel-race",
                        sequence=1,
                        direction="request",
                        message_type="execute",
                        payload_bytes=47,
                        status="accepted",
                    )
                )
                await seed_session.commit()
                claims_binding = EgoBrowserRelayBinding(
                    user_id=user.id,
                    ego_browser_device_id=device.id,
                    tool_session_id=binding.binding_tool_session_id,
                    binding_id=binding.id,
                    node_id=node.id,
                    generation=1,
                )
                binding_id = binding.id

            response_claims = EgoBrowserRelayTicketClaims(
                binding=claims_binding,
                role="bridge",
            )
            cancel_claims = EgoBrowserRelayTicketClaims(
                binding=claims_binding,
                role="wrapper",
            )
            base_envelope: dict[str, object] = {
                "protocol": "ego-browser-bridge-v1",
                "channel": "ego_browser_bridge",
                "relay_binding_kind": "ego_browser",
                "binding_id": str(binding_id),
                "generation": 1,
                "request_id": "request-response-cancel-race",
                "sequence": 1,
                "payload_bytes": 31,
                "key_wrap": "",
            }
            response_envelope = {
                **base_envelope,
                "direction": "response",
                "type": "execute_result",
            }
            cancel_envelope = {
                **base_envelope,
                "direction": "request",
                "type": "cancel",
            }

            lock_acquired = asyncio.Event()
            release_lock = asyncio.Event()
            settings = Settings(ego_browser_bridge_enabled=True)
            async with session_factory() as response_session, session_factory() as cancel_session:
                response_service = EgoBrowserService(response_session, settings)
                cancel_service = EgoBrowserService(cancel_session, settings)
                first_service = (
                    response_service if first_operation == "response" else cancel_service
                )
                original_get_binding = first_service._repository.get_binding  # noqa: SLF001

                async def get_binding_with_barrier(
                    current_binding_id: UUID,
                    *,
                    for_update: bool = False,
                ) -> EgoBrowserBinding | None:
                    current = await original_get_binding(
                        current_binding_id,
                        for_update=for_update,
                    )
                    if for_update:
                        lock_acquired.set()
                        await release_lock.wait()
                    return current

                monkeypatch.setattr(
                    first_service._repository,  # noqa: SLF001
                    "get_binding",
                    get_binding_with_barrier,
                )

                async def admit_response() -> None:
                    await response_service.admit_outer_envelope(
                        claims=response_claims,
                        envelope=response_envelope,
                    )

                async def admit_cancel() -> None:
                    await cancel_service.admit_outer_envelope(
                        claims=cancel_claims,
                        envelope=cancel_envelope,
                    )

                first = admit_response if first_operation == "response" else admit_cancel
                second = admit_cancel if first_operation == "response" else admit_response
                first_task: asyncio.Task[None] | None = None
                second_task: asyncio.Task[None] | None = None
                try:
                    first_task = asyncio.create_task(first())
                    await asyncio.wait_for(lock_acquired.wait(), timeout=2)
                    second_task = asyncio.create_task(second())
                    await asyncio.sleep(0.1)
                    assert not second_task.done()
                    release_lock.set()
                    await asyncio.wait_for(
                        asyncio.gather(first_task, second_task),
                        timeout=2,
                    )
                finally:
                    release_lock.set()
                    tasks = [task for task in (first_task, second_task) if task is not None]
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)

            async with session_factory() as assertion_session:
                ledgers = list(
                    await assertion_session.scalars(
                        select(EgoBrowserRequestLedger)
                        .where(EgoBrowserRequestLedger.binding_id == binding_id)
                        .order_by(EgoBrowserRequestLedger.direction)
                    )
                )
                request = next(item for item in ledgers if item.direction == "request")
                assert request.status == (
                    "completed" if first_operation == "response" else "cancelled"
                )
                assert len(ledgers) == 2
                actions = list(
                    await assertion_session.scalars(
                        select(AuditLog.action).where(AuditLog.target_id == str(binding_id))
                    )
                )
                assert actions.count("ego_browser_execute.completed") == (
                    1 if first_operation == "response" else 0
                )
                assert actions.count("ego_browser_execute.cancelled") == (
                    0 if first_operation == "response" else 1
                )
        finally:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.drop_all)
                await connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
            await engine.dispose()

    asyncio.run(scenario())


async def _seed_active_binding(
    session: AsyncSession,
) -> tuple[User, Node, EgoBrowserDevice, EgoBrowserBinding]:
    now = datetime.now(UTC)
    suffix = uuid4().hex
    user = User(
        username=f"ego-browser-race-{suffix}",
        display_name="Ego Browser Race",
        role="user",
        status="active",
        password_hash="test",
        totp_enabled=False,
    )
    node = Node(
        name=f"ego-browser-race-{suffix}",
        status="healthy",
        region_code="US",
        tags=[],
        weight=1,
        supported_tool_types=["claude"],
        runtime_capabilities={},
    )
    session.add_all([user, node])
    await session.flush()
    device = EgoBrowserDevice(
        user_id=user.id,
        public_key=base64.urlsafe_b64encode(bytes([1]) * 32).rstrip(b"=").decode("ascii"),
        encryption_public_key=base64.urlsafe_b64encode(bytes([2]) * 32)
        .rstrip(b"=")
        .decode("ascii"),
        generation=1,
        status="active",
        platform="macos",
        release_profile="logic-test",
        signer_certificate_sha256="development",
        credential_profile="community_file",
        bridge_protocol_version="ego-browser-bridge-v1",
        capabilities=list(REQUIRED_CAPABILITIES),
        allowlist_revision=1,
    )
    session.add(device)
    await session.flush()
    binding = EgoBrowserBinding(
        user_id=user.id,
        ego_browser_device_id=device.id,
        tool_session_id=None,
        tool_session_reference_id=device.id,
        node_id=node.id,
        status="active",
        control_channel="ego_browser_bridge",
        relay_binding_kind="ego_browser",
        authorization_mode="ego_browser_script_full_trust",
        authorization_policy_version=1,
        authorized_at=now,
        release_profile="logic-test",
        signer_certificate_sha256="development",
        credential_profile="community_file",
        remote_platform="linux",
        local_platform="macos",
        bridge_protocol_version="ego-browser-bridge-v1",
        allowlist_revision=1,
        concurrency_mode="binding",
        max_parallel_requests=4,
        capabilities=list(REQUIRED_CAPABILITIES),
        lease_until=now + timedelta(seconds=60),
        lease_health="healthy",
        lease_renew_interval_seconds=20,
        lease_renew_failure_grace_seconds=10,
        absolute_ttl_until=now + timedelta(hours=8),
        generation=1,
    )
    session.add(binding)
    await session.commit()
    return user, node, device, binding
