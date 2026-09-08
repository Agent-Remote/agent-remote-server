import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import cast
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
from agent_remote_server.logging import JsonFormatter
from agent_remote_server.models import (
    AuditLog,
    EgoBrowserBinding,
    EgoBrowserDevice,
    EgoBrowserRequestLedger,
    EgoBrowserRevocationOutbox,
    Node,
    NodeTask,
    User,
)
from agent_remote_server.schemas.ego_browser import (
    EgoBrowserConnectedRequest,
    EgoBrowserNodeRenewRequest,
    EgoBrowserRelayTicketRequest,
    EgoBrowserRenewRequest,
    EgoBrowserResumeRequest,
)
from agent_remote_server.services.ego_browser import (
    POLICY_CAPABILITIES,
    REQUIRED_CAPABILITIES,
    EgoBrowserService,
    _validate_digest,
)


def _node_runtime_capabilities() -> dict[str, object]:
    return {
        "ego_browser_bridge": {
            "supported": True,
            "protocol_versions": ["ego-browser-bridge-v1"],
            "backends": ["native", "docker_sandbox"],
            "wrapper_version": "0.1.0",
            "skill_version": "1.2.3",
            "skill_tree_sha256": (
                "262110a09678fd3e0bbb382400588dacb98b24659b3b4a57903703b65d133c7c"
            ),
            "remote_platform": "linux",
            "local_platform": "macos",
            "max_script_bytes": 1_048_576,
            "max_execute_timeout_ms": 120_000,
        }
    }


def test_policy_capabilities_match_verified_digests() -> None:
    """策略能力必须与对应摘要同时出现且拒绝未知或重复值。"""

    service = EgoBrowserService(
        cast(AsyncSession, None),
        Settings(ego_browser_bridge_enabled=True),
    )
    assert service._validate_policy_capabilities(  # noqa: SLF001
        REQUIRED_CAPABILITIES,
        allowlist_roots_digest=None,
        learning_bundle_digest=None,
    ) == sorted(REQUIRED_CAPABILITIES)

    digest = f"sha256:{'a' * 64}"
    complete = (*REQUIRED_CAPABILITIES, *POLICY_CAPABILITIES)
    assert service._validate_policy_capabilities(  # noqa: SLF001
        complete,
        allowlist_roots_digest=digest,
        learning_bundle_digest=digest,
    ) == sorted(complete)

    invalid_cases = [
        ((*REQUIRED_CAPABILITIES, POLICY_CAPABILITIES[0]), None, None),
        (REQUIRED_CAPABILITIES, digest, None),
        ((*REQUIRED_CAPABILITIES, REQUIRED_CAPABILITIES[0]), None, None),
        ((*REQUIRED_CAPABILITIES, "unknown_capability_v1"), None, None),
    ]
    for capabilities, roots_digest, learning_digest in invalid_cases:
        with pytest.raises(ApiError) as raised:
            service._validate_policy_capabilities(  # noqa: SLF001
                capabilities,
                allowlist_roots_digest=roots_digest,
                learning_bundle_digest=learning_digest,
            )
        assert raised.value.code == "EGO_BROWSER_CAPABILITY_MISMATCH"


def test_production_requires_policy_backed_capabilities() -> None:
    """生产环境不得激活缺少 allowlist 或学习 bundle 的 Bridge。"""

    settings = Settings().model_copy(
        update={"environment": "production", "ego_browser_bridge_enabled": True}
    )
    service = EgoBrowserService(cast(AsyncSession, None), settings)
    with pytest.raises(ApiError) as raised:
        service._validate_policy_capabilities(  # noqa: SLF001
            REQUIRED_CAPABILITIES,
            allowlist_roots_digest=None,
            learning_bundle_digest=None,
        )
    assert raised.value.code == "EGO_BROWSER_CAPABILITY_MISMATCH"


def test_digest_requires_canonical_sha256_prefix() -> None:
    """摘要必须使用跨组件一致的 sha256 前缀和小写十六进制。"""

    service = EgoBrowserService(cast(AsyncSession, None), Settings())
    _validate_digest(f"sha256:{'a' * 64}", service._error)  # noqa: SLF001

    for digest in ("a" * 64, f"sha256:{'A' * 64}", "sha256:short"):
        with pytest.raises(ApiError) as raised:
            _validate_digest(digest, service._error)  # noqa: SLF001
        assert raised.value.code == "EGO_BROWSER_DIGEST_INVALID"


def test_node_capability_pins_wrapper_and_official_skill_artifacts() -> None:
    """Node capability 必须完整匹配 wrapper、官方 Skill、平台和执行限额。"""

    service = EgoBrowserService(cast(AsyncSession, None), Settings())
    valid = {
        "supported": True,
        "protocol_versions": ["ego-browser-bridge-v1"],
        "backends": ["native", "docker_sandbox"],
        "wrapper_version": "0.1.0",
        "skill_version": "1.2.3",
        "skill_tree_sha256": ("262110a09678fd3e0bbb382400588dacb98b24659b3b4a57903703b65d133c7c"),
        "remote_platform": "linux",
        "local_platform": "macos",
        "max_script_bytes": 1_048_576,
        "max_execute_timeout_ms": 120_000,
    }
    node = Node(runtime_capabilities={"ego_browser_bridge": valid})
    service._validate_node_capability(node, runtime_backend="native")  # noqa: SLF001
    service._validate_node_capability(node, runtime_backend="docker_sandbox")  # noqa: SLF001

    invalid_cases = [
        ({**valid, "skill_version": "latest"}, "EGO_BROWSER_VERSION_MISMATCH"),
        (
            {**valid, "skill_tree_sha256": "a" * 64},
            "EGO_BROWSER_VERSION_MISMATCH",
        ),
        ({**valid, "remote_platform": "darwin"}, "EGO_BROWSER_VERSION_MISMATCH"),
        ({**valid, "max_script_bytes": 1_048_577}, "EGO_BROWSER_CAPABILITY_MISMATCH"),
        ({**valid, "unknown": True}, "EGO_BROWSER_NODE_UNAVAILABLE"),
    ]
    for capability, expected_code in invalid_cases:
        node.runtime_capabilities = {"ego_browser_bridge": capability}
        with pytest.raises(ApiError) as raised:
            service._validate_node_capability(node, runtime_backend="native")  # noqa: SLF001
        assert raised.value.code == expected_code


def test_signer_pin_never_accepts_missing_evidence() -> None:
    """配置 signer pin 后必须提供完全一致的证书摘要。"""

    service = EgoBrowserService(
        cast(AsyncSession, None),
        Settings(
            ego_browser_bridge_enabled=True,
            ego_browser_expected_signer_certificate_sha256="a" * 64,
        ),
    )
    with pytest.raises(ApiError) as raised:
        service._validate_profile(  # noqa: SLF001
            release_profile="community-local-trust",
            credential_profile="community_file",
            signer_certificate_sha256=None,
        )
    assert raised.value.code == "EGO_BROWSER_SIGNER_MISMATCH"


def test_profile_pin_drift_blocks_activation_renewal_ticket_and_reconnect() -> None:
    """所有授予能力的路径都必须重新校验当前发布身份策略。"""

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                now = datetime.now(UTC)
                user = User(
                    username="profile-drift-owner",
                    display_name="Profile Drift Owner",
                    role="user",
                    status="active",
                    password_hash="test",
                    totp_enabled=False,
                )
                node = Node(
                    name="profile-drift-node",
                    status="healthy",
                    region_code="US",
                    tags=[],
                    weight=1,
                    supported_tool_types=["claude"],
                    runtime_capabilities=_node_runtime_capabilities(),
                )
                session.add_all([user, node])
                await session.flush()
                device = EgoBrowserDevice(
                    user_id=user.id,
                    public_key="A" * 43,
                    encryption_public_key="B" * 43,
                    generation=1,
                    status="active",
                    platform="macos",
                    release_profile="community-local-trust",
                    signer_certificate_sha256="a" * 64,
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
                    release_profile=device.release_profile,
                    signer_certificate_sha256=device.signer_certificate_sha256,
                    credential_profile=device.credential_profile,
                    remote_platform="linux",
                    local_platform="macos",
                    bridge_protocol_version="ego-browser-bridge-v1",
                    allowlist_revision=1,
                    concurrency_mode="binding",
                    max_parallel_requests=4,
                    capabilities=list(REQUIRED_CAPABILITIES),
                    lease_until=now + timedelta(minutes=1),
                    lease_health="healthy",
                    lease_renew_interval_seconds=20,
                    lease_renew_failure_grace_seconds=10,
                    absolute_ttl_until=now + timedelta(hours=1),
                    generation=1,
                )
                session.add(binding)
                await session.commit()
                claims = EgoBrowserRelayTicketClaims(
                    binding=EgoBrowserRelayBinding(
                        user_id=user.id,
                        ego_browser_device_id=device.id,
                        tool_session_id=device.id,
                        binding_id=binding.id,
                        node_id=node.id,
                        generation=1,
                    ),
                    role="wrapper",
                )
                current = EgoBrowserService(
                    session,
                    Settings(
                        ego_browser_bridge_enabled=True,
                        ego_browser_expected_signer_certificate_sha256="a" * 64,
                    ),
                )
                assert await current.relay_claims_are_current(claims)

                drifted = EgoBrowserService(
                    session,
                    Settings(
                        ego_browser_bridge_enabled=True,
                        ego_browser_expected_signer_certificate_sha256="b" * 64,
                    ),
                )
                assert not await drifted.relay_claims_are_current(claims)
                operations = [
                    drifted.connected(
                        user=user,
                        binding_id=binding.id,
                        payload=EgoBrowserConnectedRequest.model_construct(),
                        device_id=device.id,
                    ),
                    drifted.renew(
                        user=user,
                        binding_id=binding.id,
                        payload=EgoBrowserRenewRequest.model_construct(),
                        device_id=device.id,
                    ),
                    drifted.renew_for_node(
                        node=node,
                        binding_id=binding.id,
                        payload=EgoBrowserNodeRenewRequest.model_construct(),
                    ),
                    drifted.resume(
                        user=user,
                        binding_id=binding.id,
                        payload=EgoBrowserResumeRequest.model_construct(),
                        device_id=device.id,
                    ),
                    drifted.issue_relay_ticket(
                        binding_id=binding.id,
                        payload=EgoBrowserRelayTicketRequest(generation=1, role="wrapper"),
                        role="wrapper",
                        node=node,
                    ),
                ]
                for operation in operations:
                    with pytest.raises(ApiError) as raised:
                        await operation
                    assert raised.value.code == "EGO_BROWSER_SIGNER_MISMATCH"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("renewal_role", ["device", "node"])
def test_expired_healthy_lease_cannot_be_renewed(renewal_role: str) -> None:
    """已经越过截止时间的健康租约不能被续租复活。"""

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                now = datetime.now(UTC)
                user = User(
                    username="ego-browser-renewal",
                    display_name="Ego Browser Renewal",
                    role="user",
                    status="active",
                    password_hash="test",
                    totp_enabled=False,
                )
                node = Node(
                    name="ego-browser-node",
                    status="healthy",
                    region_code="US",
                    tags=[],
                    weight=1,
                    supported_tool_types=["claude"],
                    runtime_capabilities=_node_runtime_capabilities(),
                )
                session.add_all([user, node])
                await session.flush()
                device = EgoBrowserDevice(
                    user_id=user.id,
                    public_key="A" * 43,
                    encryption_public_key="B" * 43,
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
                    authorized_at=now - timedelta(minutes=1),
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
                    lease_until=now - timedelta(seconds=11),
                    lease_health="healthy",
                    lease_renew_interval_seconds=20,
                    lease_renew_failure_grace_seconds=10,
                    absolute_ttl_until=now + timedelta(hours=1),
                    generation=1,
                )
                session.add(binding)
                await session.commit()

                service = EgoBrowserService(
                    session,
                    Settings(ego_browser_bridge_enabled=True),
                )
                with pytest.raises(ApiError) as raised:
                    if renewal_role == "device":
                        await service.renew(
                            user=user,
                            binding_id=binding.id,
                            payload=EgoBrowserRenewRequest(
                                generation=1,
                                allowlist_revision=1,
                                learning_bundle_digest=None,
                            ),
                            device_id=device.id,
                        )
                    else:
                        await service.renew_for_node(
                            node=node,
                            binding_id=binding.id,
                            payload=EgoBrowserNodeRenewRequest(
                                generation=1,
                                allowlist_revision=1,
                                learning_bundle_digest=None,
                            ),
                        )

                assert raised.value.code == "EGO_BROWSER_LEASE_EXPIRED"
                assert binding.status == "expired"
                assert binding.generation == 2
                assert binding.lease_until is None
                outbox = list(await session.scalars(select(EgoBrowserRevocationOutbox)))
                assert [(item.generation, item.reason) for item in outbox] == [
                    (1, "renewal_grace_expired")
                ]
                audits = list(
                    await session.scalars(
                        select(AuditLog).where(AuditLog.action == "ego_browser_binding.revoked")
                    )
                )
                assert len(audits) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_revocation_sources_expiry_and_renewal_failure_are_idempotent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """所有外部停止来源都必须通过共享 outbox 使 generation 失效。"""

    class RecordingPublisher:
        def __init__(self) -> None:
            self.events: list[tuple[UUID, int]] = []

        async def publish(self, binding_id: UUID, generation: int) -> None:
            self.events.append((binding_id, generation))

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                first_user = User(
                    username="revocation-owner",
                    display_name="Revocation Owner",
                    role="user",
                    status="active",
                    password_hash="test",
                    totp_enabled=False,
                )
                second_user = User(
                    username="revocation-second",
                    display_name="Revocation Second",
                    role="user",
                    status="active",
                    password_hash="test",
                    totp_enabled=False,
                )
                node = Node(
                    name="revocation-node",
                    status="healthy",
                    region_code="US",
                    tags=[],
                    weight=1,
                    supported_tool_types=["claude"],
                    runtime_capabilities={},
                )
                session.add_all([first_user, second_user, node])
                await session.flush()

                async def add_binding(
                    *,
                    owner: User,
                    reference_id: UUID,
                    lease_until: datetime | None = None,
                    absolute_ttl_until: datetime | None = None,
                ) -> tuple[EgoBrowserDevice, EgoBrowserBinding]:
                    now = datetime.now(UTC)
                    device = EgoBrowserDevice(
                        user_id=owner.id,
                        public_key="A" * 43,
                        encryption_public_key=("AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"),
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
                        user_id=owner.id,
                        ego_browser_device_id=device.id,
                        tool_session_id=None,
                        tool_session_reference_id=reference_id,
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
                        lease_until=lease_until or now + timedelta(seconds=60),
                        lease_health="healthy",
                        lease_renew_interval_seconds=20,
                        lease_renew_failure_grace_seconds=10,
                        absolute_ttl_until=absolute_ttl_until or now + timedelta(hours=8),
                        generation=1,
                    )
                    session.add(binding)
                    await session.commit()
                    return device, binding

                publisher = RecordingPublisher()
                service = EgoBrowserService(
                    session,
                    Settings(ego_browser_bridge_enabled=True),
                    revocation_publisher=publisher,
                )

                tool_reference = uuid4()
                _, tool_binding = await add_binding(
                    owner=first_user,
                    reference_id=tool_reference,
                )
                assert (
                    await service.revoke_for_tool_session(
                        tool_session_id=tool_reference,
                        reason="tool_session_stopped",
                    )
                    == 1
                )
                assert (
                    await service.revoke_for_tool_session(
                        tool_session_id=tool_reference,
                        reason="tool_session_stopped",
                    )
                    == 0
                )
                assert (tool_binding.status, tool_binding.generation) == ("revoked", 2)

                device, device_binding = await add_binding(
                    owner=first_user,
                    reference_id=uuid4(),
                )
                assert (
                    await service.revoke_for_device(
                        device_id=device.id,
                        reason="device_revoked",
                    )
                    == 1
                )
                assert (
                    await service.revoke_for_device(
                        device_id=device.id,
                        reason="device_revoked",
                    )
                    == 0
                )
                assert device_binding.status == "revoked"

                _, user_binding = await add_binding(
                    owner=second_user,
                    reference_id=uuid4(),
                )
                assert (
                    await service.revoke_for_user(
                        user_id=second_user.id,
                        reason="user_disabled",
                    )
                    == 1
                )
                assert (
                    await service.revoke_for_user(
                        user_id=second_user.id,
                        reason="user_disabled",
                    )
                    == 0
                )
                assert user_binding.status == "revoked"

                _, node_binding = await add_binding(
                    owner=first_user,
                    reference_id=uuid4(),
                )
                assert (
                    await service.revoke_for_node(
                        node_id=node.id,
                        reason="node_unavailable",
                    )
                    == 1
                )
                assert (
                    await service.revoke_for_node(
                        node_id=node.id,
                        reason="node_unavailable",
                    )
                    == 0
                )
                assert node_binding.status == "revoked"

                now = datetime.now(UTC)
                _, expired_binding = await add_binding(
                    owner=first_user,
                    reference_id=uuid4(),
                    lease_until=now - timedelta(seconds=1),
                )
                assert await service.expire_due() == 0
                assert expired_binding.lease_health == "renewal_grace"
                assert expired_binding.lease_grace_until is not None
                expired_binding.lease_grace_until = now - timedelta(seconds=1)
                await session.commit()
                assert await service.expire_due() == 1
                assert await service.expire_due() == 0
                assert (expired_binding.status, expired_binding.stop_reason) == (
                    "expired",
                    "renewal_grace_expired",
                )

                _, grace_binding = await add_binding(
                    owner=first_user,
                    reference_id=uuid4(),
                )
                failed = await service.mark_renewal_failed(
                    binding_id=grace_binding.id,
                    generation=1,
                )
                assert failed is grace_binding
                assert grace_binding.lease_health == "renewal_grace"
                assert grace_binding.lease_grace_until is not None
                assert (
                    await service.mark_renewal_failed(
                        binding_id=grace_binding.id,
                        generation=2,
                    )
                    is None
                )

                grace_binding.absolute_ttl_until = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()
                expired = await service.mark_renewal_failed(
                    binding_id=grace_binding.id,
                    generation=1,
                )
                assert expired is grace_binding
                assert (grace_binding.status, grace_binding.generation) == ("expired", 2)

                assert len(publisher.events) == 6
                outbox = list(await session.scalars(select(EgoBrowserRevocationOutbox)))
                assert len(outbox) == 6
                assert all(item.delivered_at is not None for item in outbox)
        finally:
            await engine.dispose()

    with caplog.at_level(logging.INFO, logger="agent_remote_server.services.ego_browser"):
        asyncio.run(scenario())

    metrics = [
        record
        for record in caplog.records
        if getattr(record, "metric_name", "") == "ego_browser_revocations_total"
    ]
    assert len(metrics) == 6
    assert {getattr(record, "revocation_reason", "") for record in metrics} == {
        "absolute_ttl",
        "device",
        "lease",
        "node",
        "tool_session",
        "user",
    }
    rendered = json.dumps([json.loads(JsonFormatter().format(record)) for record in metrics])
    assert "revocation-owner" not in rendered
    assert "revocation-second" not in rendered
    assert all(
        json.loads(JsonFormatter().format(record))["metric_value"] == 1 for record in metrics
    )


def test_cancel_task_terminal_ack_converges_without_revoking_binding() -> None:
    """Node 已观察 Server 终态时只收敛 request，不扩大为 binding 撤销。"""

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                now = datetime.now(UTC)
                user = User(
                    username="cancel-ack-owner",
                    display_name="Cancel Ack Owner",
                    role="user",
                    status="active",
                    password_hash="test",
                    totp_enabled=False,
                )
                node = Node(
                    name="cancel-ack-node",
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
                    public_key="A" * 43,
                    encryption_public_key=("AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"),
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
                    tool_session_reference_id=uuid4(),
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
                await session.flush()
                request = EgoBrowserRequestLedger(
                    binding_id=binding.id,
                    generation=1,
                    request_id="cancel-ack-request",
                    sequence=9,
                    direction="request",
                    message_type="execute",
                    payload_bytes=64,
                    status="cancel_requested",
                )
                task = NodeTask(
                    node_id=node.id,
                    task_id=f"cancel_ego_browser_request:{request.id}",
                    task_type="cancel_ego_browser_request",
                    status="succeeded",
                    payload={
                        "binding_id": str(binding.id),
                        "generation": 1,
                        "request_id": request.request_id,
                        "sequence": request.sequence,
                    },
                    retry_count=1,
                )
                session.add_all([request, task])
                await session.commit()

                await EgoBrowserService(
                    session,
                    Settings(ego_browser_bridge_enabled=True),
                ).reconcile_cancel_task(
                    task=task,
                    result={
                        "status": "cancellation_completed",
                        "request_active": True,
                        "server_terminal_observed": True,
                    },
                    succeeded=True,
                )
                await session.commit()

                assert request.status == "cancelled"
                assert (binding.status, binding.generation) == ("active", 1)
                assert list(await session.scalars(select(EgoBrowserRevocationOutbox))) == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_revocation_metric_maps_untrusted_reason_to_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """用户提供的生命周期文本不得进入指标标签或消息。"""

    from agent_remote_server.services.ego_browser import _record_revocation_metric, _safe_reason

    with caplog.at_level(logging.INFO, logger="agent_remote_server.services.ego_browser"):
        _record_revocation_metric("local_policy_changed")

    assert caplog.records[-1].revocation_reason == "policy"  # type: ignore[attr-defined]

    untrusted = "customer-name-and-local-path"
    assert _safe_reason(untrusted) == "other"
    assert _safe_reason("task_space_takeover") == "task_space_takeover"
    with caplog.at_level(logging.INFO, logger="agent_remote_server.services.ego_browser"):
        _record_revocation_metric(untrusted)

    record = caplog.records[-1]
    assert record.revocation_reason == "other"  # type: ignore[attr-defined]
    structured = json.loads(JsonFormatter().format(record))
    assert structured["revocation_reason"] == "other"
    assert untrusted not in json.dumps(structured)


def test_user_and_admin_lifecycle_controls_only_remove_capability() -> None:
    """普通用户和管理员可降权控制，但其他用户不能读取或修改 binding。"""

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                now = datetime.now(UTC)
                owner = User(
                    username="ego-owner",
                    display_name="Ego Owner",
                    role="user",
                    status="active",
                    password_hash="test",
                    totp_enabled=False,
                )
                stranger = User(
                    username="ego-stranger",
                    display_name="Ego Stranger",
                    role="user",
                    status="active",
                    password_hash="test",
                    totp_enabled=False,
                )
                admin = User(
                    username="ego-admin",
                    display_name="Ego Admin",
                    role="admin",
                    status="active",
                    password_hash="test",
                    totp_enabled=False,
                )
                node = Node(
                    name="ego-lifecycle-node",
                    status="healthy",
                    region_code="US",
                    tags=[],
                    weight=1,
                    supported_tool_types=["claude"],
                    runtime_capabilities={},
                )
                session.add_all([owner, stranger, admin, node])
                await session.flush()
                device = EgoBrowserDevice(
                    user_id=owner.id,
                    public_key="A" * 43,
                    encryption_public_key="B" * 43,
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
                    user_id=owner.id,
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

                settings = Settings(
                    ego_browser_bridge_enabled=True,
                    ego_browser_require_device_pop=True,
                )
                service = EgoBrowserService(session, settings)
                with pytest.raises(ApiError) as raised:
                    await service.get_binding(user=stranger, binding_id=binding.id)
                assert raised.value.code == "EGO_BROWSER_BINDING_NOT_FOUND"
                with pytest.raises(ApiError) as raised:
                    await service.list_bindings(user=owner, all_users=True)
                assert raised.value.code == "COMMON_FORBIDDEN"

                paused = await service.pause_by_user(
                    user=owner,
                    binding_id=binding.id,
                    generation=1,
                )
                assert (paused.status, paused.generation) == ("paused", 2)
                assert await service.get_binding(user=admin, binding_id=binding.id) is binding
                assert await service.list_bindings(user=admin, all_users=True) == [binding]
                assert await service.list_devices(user=admin, all_users=True) == [device]

                stopped = await service.stop_by_user(
                    user=admin,
                    binding_id=binding.id,
                    generation=2,
                    reason="admin_cleanup",
                )
                assert (stopped.status, stopped.generation) == ("stopped", 3)

                revoked = await service.stop_by_user(
                    user=admin,
                    binding_id=binding.id,
                    generation=3,
                    reason="admin_revoke",
                    revoke=True,
                )
                assert (revoked.status, revoked.generation) == ("revoked", 4)
                assert revoked.stop_reason == "admin_revoke"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
