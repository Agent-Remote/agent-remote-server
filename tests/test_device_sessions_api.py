import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.websockets import WebSocketDisconnect
from test_sessions_api import (
    auth_header,
    bootstrap,
    create_account,
    create_node,
    create_workspace,
    register_device,
)

from agent_remote_server import __version__
from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.device_control_limits import (
    MAX_ACTIVE_DEVICE_SESSION_GENERATION,
    MAX_DEVICE_SESSION_GENERATION,
)
from agent_remote_server.device_control_release import DeviceControlReleaseEvidence
from agent_remote_server.device_relay_store import InMemoryDeviceRelayStore
from agent_remote_server.errors import ApiError
from agent_remote_server.main import create_app
from agent_remote_server.models import (
    AuditLog,
    AuthToken,
    DeviceSession,
    DeviceSessionApproval,
    Node,
    NodeTask,
    Session,
    User,
    UserDevice,
)
from agent_remote_server.schemas.device_sessions import DeviceApprovalItem
from agent_remote_server.security import hash_token
from agent_remote_server.services.device_sessions import DeviceSessionService

_DIGEST = "a" * 64


@pytest.mark.parametrize(
    "advertised",
    [
        ["ax_state_v2"],
        ["adaptive_settle_v2", "ax_state_v2"],
        "adaptive_settle_v2",
        ["adaptive_settle_v2", 7, "observation_mode_v2"],
        [
            "adaptive_settle_v2",
            "ax_state_v2",
            "observation_mode_v2",
            "unknown_v2",
        ],
        [
            "adaptive_settle_v2",
            "ax_state_v2",
            "observation_mode_v2",
            "observation_mode_v2",
        ],
        None,
    ],
)
def test_device_session_v2_capabilities_fail_closed_when_incomplete_or_malformed(
    advertised: object,
) -> None:
    """部分或畸形 capability 不能启用任何 v2 语义。"""

    service = DeviceSessionService.__new__(DeviceSessionService)
    service._settings = Settings(secret_key="test-secret")
    assert (
        service._negotiated_v2_capabilities({"device_control": {"capabilities": advertised}}) == ()
    )


def test_device_session_v2_capabilities_are_canonicalized() -> None:
    """完整无序集合只输出控制面定义的规范顺序。"""

    service = DeviceSessionService.__new__(DeviceSessionService)
    service._settings = Settings(secret_key="test-secret")
    assert service._negotiated_v2_capabilities(
        {
            "device_control": {
                "capabilities": [
                    "observation_mode_v2",
                    "clipboard_payload_v2",
                    "adaptive_settle_v2",
                    "ax_state_v2",
                ]
            }
        },
    ) == (
        "adaptive_settle_v2",
        "ax_state_v2",
        "clipboard_payload_v2",
        "observation_mode_v2",
    )


def test_device_session_v2_is_default_and_emergency_switch_falls_back_to_v1() -> None:
    """完整能力默认协商 v2，紧急开关关闭后原子回退 v1。"""

    runtime_capabilities: dict[str, object] = {
        "device_control": {
            "capabilities": [
                "adaptive_settle_v2",
                "ax_state_v2",
                "clipboard_payload_v2",
                "observation_mode_v2",
            ]
        }
    }
    service = DeviceSessionService.__new__(DeviceSessionService)
    service._settings = Settings(secret_key="test-secret")
    assert service._negotiated_v2_capabilities(runtime_capabilities) == (
        "adaptive_settle_v2",
        "ax_state_v2",
        "clipboard_payload_v2",
        "observation_mode_v2",
    )

    service._settings = Settings(
        secret_key="test-secret",
        device_control_v2_enabled=False,
    )
    assert service._negotiated_v2_capabilities(runtime_capabilities) == ()


async def create_schema(app: FastAPI) -> None:
    """创建测试数据库 schema。"""

    async with app.state.database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """创建使用独立内存数据库的测试客户端。"""

    settings = Settings(
        secret_key="test-secret",
        log_level="CRITICAL",
        database_url="sqlite+aiosqlite:///:memory:",
        node_task_lease_seconds=30,
        node_offline_after_seconds=60,
        device_control_enabled=True,
    )
    app = create_app(settings)
    app.state.device_relay_store = InMemoryDeviceRelayStore()
    asyncio.run(create_schema(app))
    with TestClient(app) as test_client:
        yield test_client


def create_running_tool_session(
    client: TestClient,
    token: str,
    *,
    project_key: str,
) -> str:
    """创建测试所需的远端运行 session。"""

    create_node(client, token, name=f"node-{project_key[-6:]}", weight=10)
    device_id, device_token = register_device(client, token)
    workspace_id = create_workspace(client, device_token, device_id, project_key)
    account_id = create_account(client, token)
    response = client.post(
        "/api/v1/sessions",
        headers=auth_header(token),
        json={
            "tool_type": "claude",
            "tool_account_id": account_id,
            "workspace_id": workspace_id,
            "project_key": project_key,
            "argv": [],
        },
    )
    assert response.status_code == 200
    tool_session_id = str(response.json()["data"]["id"])

    async def mark_running() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            tool_session = await session.get(Session, UUID(tool_session_id))
            assert tool_session is not None
            tool_session.status = "running"
            tool_session.device_control_protocol_version = 1
            node = await session.get(Node, tool_session.node_id)
            assert node is not None
            node.runtime_capabilities = {
                "device_control": {
                    "supported": True,
                    "protocol_versions": [1],
                    "platforms": ["macos"],
                    "backends": [tool_session.runtime_backend],
                }
            }
            await session.commit()

    asyncio.run(mark_running())
    return tool_session_id


def expired_release_evidence() -> DeviceControlReleaseEvidence:
    """构造仅用于运行期过期门禁测试的发布证据。"""

    now = datetime.now(UTC)
    return DeviceControlReleaseEvidence(
        schema_version=1,
        release_version=__version__,
        issued_at=now - timedelta(days=2),
        expires_at=now - timedelta(seconds=1),
        server_sha256=_DIGEST,
        node_sha256=_DIGEST,
        application_sha256=_DIGEST,
        proxy_sha256=_DIGEST,
        sbom_sha256=_DIGEST,
        provenance_sha256=_DIGEST,
        security_tests_sha256=_DIGEST,
        security_review_sha256=_DIGEST,
        signing_notarization_sha256=_DIGEST,
        outbound_policy_sha256=_DIGEST,
        local_claude_isolation_sha256=_DIGEST,
        stop_revocation_sha256=_DIGEST,
        compatibility_sha256=_DIGEST,
        ci_run_url="https://ci.example.test/runs/expired",
        signature="test-only",
    )


def current_release_evidence_without_v2() -> DeviceControlReleaseEvidence:
    """构造当前有效但未批准 Computer Use v2 的 Apple 发布证据。"""

    now = datetime.now(UTC)
    return DeviceControlReleaseEvidence(
        schema_version=1,
        release_version=__version__,
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
        server_sha256=_DIGEST,
        node_sha256=_DIGEST,
        application_sha256=_DIGEST,
        proxy_sha256=_DIGEST,
        sbom_sha256=_DIGEST,
        provenance_sha256=_DIGEST,
        security_tests_sha256=_DIGEST,
        security_review_sha256=_DIGEST,
        signing_notarization_sha256=_DIGEST,
        outbound_policy_sha256=_DIGEST,
        local_claude_isolation_sha256=_DIGEST,
        stop_revocation_sha256=_DIGEST,
        compatibility_sha256=_DIGEST,
        ci_run_url="https://ci.example.test/runs/current-without-v2",
        signature="test-only",
    )


def test_expired_runtime_release_evidence_blocks_progress_but_allows_stop(
    client: TestClient,
) -> None:
    """运行期证据过期应拒绝推进会话但不能阻断安全停止。"""

    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client,
        token,
        project_key="sha256:expired-release-evidence",
    )
    device_id, device_token = register_device(client, token)
    created = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    assert created.status_code == 200
    device_session_id = created.json()["data"]["id"]

    app = cast(FastAPI, client.app)
    app.state.settings.environment = "production"
    app.state.device_control_release_evidence = expired_release_evidence()

    connected = client.post(
        f"/api/v1/device-sessions/{device_session_id}/device-connected",
        headers=auth_header(device_token),
        json={"generation": 1},
    )
    assert connected.status_code == 503
    assert connected.json()["error"]["code"] == "DEVICE_CONTROL_RELEASE_EVIDENCE_EXPIRED"

    stopped = client.post(
        f"/api/v1/device-sessions/{device_session_id}/stop",
        headers=auth_header(token),
        json={"reason": "lease_expired"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["data"]["status"] == "stopped"


def test_runtime_v2_does_not_require_specialized_release_evidence(client: TestClient) -> None:
    """当前通用生产证据允许完整能力集合自动协商 v2。"""

    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client,
        token,
        project_key="sha256:runtime-v2-release-evidence",
    )
    device_id, device_token = register_device(client, token)
    created = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    assert created.status_code == 200
    device_session_id = created.json()["data"]["id"]

    app = cast(FastAPI, client.app)
    app.state.settings.environment = "production"
    app.state.device_control_release_evidence = current_release_evidence_without_v2()

    connected = client.post(
        f"/api/v1/device-sessions/{device_session_id}/device-connected",
        headers=auth_header(device_token),
        json={"generation": 1},
    )

    assert connected.status_code == 200


def test_device_session_lifecycle_is_device_bound_and_fail_closed(client: TestClient) -> None:
    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:device-control"
    )
    device_id, device_token = register_device(client, token)
    other_device_id, other_device_token = register_device(client, token)

    created = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    assert created.status_code == 200
    body = created.json()["data"]
    device_session_id = str(body["id"])
    assert body["status"] == "pending_device"
    assert body["generation"] == 1
    assert "endpoint" not in body

    async def load_activation_task() -> NodeTask:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            task = await session.scalar(
                select(NodeTask).where(
                    NodeTask.task_id == f"activate_device_control:{device_session_id}:1"
                )
            )
            assert task is not None
            return task

    activation_task = asyncio.run(load_activation_task())
    assert activation_task.task_type == "activate_device_control"
    assert activation_task.payload == {
        "protocol_version": 1,
        "user_id": str(body["user_id"]),
        "device_id": device_id,
        "tool_session_id": tool_session_id,
        "device_session_id": device_session_id,
        "node_id": str(body["node_id"]),
        "platform": "macos",
        "generation": 1,
        "expires_at": body["expires_at"],
        "runtime_backend": "docker_sandbox",
        "runtime_resource_id": None,
    }

    wrong_device = client.post(
        f"/api/v1/device-sessions/{device_session_id}/device-connected",
        headers=auth_header(other_device_token),
        json={"generation": 1},
    )
    assert wrong_device.status_code == 404
    assert other_device_id != device_id

    connected = client.post(
        f"/api/v1/device-sessions/{device_session_id}/device-connected",
        headers=auth_header(device_token),
        json={"generation": 1},
    )
    assert connected.status_code == 200
    assert connected.json()["data"]["status"] == "pending_user_approval"

    digest = "a" * 64
    approved = client.post(
        f"/api/v1/device-sessions/{device_session_id}/approve",
        headers=auth_header(device_token),
        json={
            "generation": 1,
            "approvals": [
                {
                    "application_digest": digest,
                    "control_level": "click_only",
                    "approval_result": "allowed",
                    "clipboard_allowed": False,
                }
            ],
        },
    )
    assert approved.status_code == 200
    approved_data = approved.json()["data"]
    assert approved_data["status"] == "active"
    assert approved_data["lease_until"] is not None
    assert digest not in approved.text

    old_generation = client.post(
        f"/api/v1/device-sessions/{device_session_id}/renew",
        headers=auth_header(device_token),
        json={"generation": 2},
    )
    assert old_generation.status_code == 409
    assert old_generation.json()["error"]["code"] == "DEVICE_CONTROL_GENERATION_MISMATCH"

    locked = client.post(
        f"/api/v1/device-sessions/{device_session_id}/lock",
        headers=auth_header(device_token),
        json={"generation": 1},
    )
    assert locked.status_code == 200
    assert locked.json()["data"]["lock_acquired_at"] is not None

    aborted = client.post(
        f"/api/v1/device-sessions/{device_session_id}/abort",
        headers=auth_header(device_token),
        json={"generation": 1, "reason": "esc"},
    )
    assert aborted.status_code == 200
    aborted_data = aborted.json()["data"]
    assert aborted_data["status"] == "pending_device"
    assert aborted_data["generation"] == 2
    assert aborted_data["lease_until"] is None
    assert aborted_data["lock_acquired_at"] is not None

    stopped = client.post(
        f"/api/v1/device-sessions/{device_session_id}/stop",
        headers=auth_header(token),
        json={"reason": "session_end"},
    )
    assert stopped.status_code == 200
    stopped_data = stopped.json()["data"]
    assert stopped_data["status"] == "stopped"
    assert stopped_data["generation"] == 3
    assert stopped_data["lease_until"] is None
    assert stopped_data["lock_acquired_at"] is None
    assert stopped_data["stop_reason"] == "session_end"

    async def verify_persistence() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            approval = await session.scalar(
                select(DeviceSessionApproval).where(
                    DeviceSessionApproval.device_session_id == UUID(device_session_id)
                )
            )
            assert approval is not None
            assert approval.application_digest == digest
            audits = list(
                await session.scalars(
                    select(AuditLog).where(AuditLog.target_id == device_session_id)
                )
            )
            assert audits
            serialized_details = repr([item.details for item in audits])
            assert digest not in serialized_details
            assert "window" not in serialized_details
            assert "text" not in serialized_details
            lifecycle_tasks = list(
                await session.scalars(
                    select(NodeTask)
                    .where(NodeTask.task_id.contains(device_session_id))
                    .order_by(NodeTask.created_at)
                )
            )
            assert [task.task_type for task in lifecycle_tasks] == [
                "activate_device_control",
                "update_device_control_context",
                "activate_device_control",
                "deactivate_device_control",
            ]
            assert lifecycle_tasks[0].task_id == (f"activate_device_control:{device_session_id}:1")
            assert lifecycle_tasks[1].task_id.startswith(
                f"update_device_control_context:{device_session_id}:1:"
            )
            assert lifecycle_tasks[1].payload == {
                "protocol_version": 1,
                "user_id": str(body["user_id"]),
                "device_id": device_id,
                "tool_session_id": tool_session_id,
                "device_session_id": device_session_id,
                "node_id": str(body["node_id"]),
                "platform": "macos",
                "generation": 1,
                "lease_until": approved_data["lease_until"],
                "capabilities": [],
            }
            assert lifecycle_tasks[2].task_id == (f"activate_device_control:{device_session_id}:2")
            assert lifecycle_tasks[3].task_id == (
                f"deactivate_device_control:{device_session_id}:3"
            )
            serialized_payloads = repr([task.payload for task in lifecycle_tasks])
            for forbidden in ("relay_ticket", "spki", "exporter", "private_key"):
                assert forbidden not in serialized_payloads

    asyncio.run(verify_persistence())


def test_device_can_list_candidates_and_rebind_a_running_claude_session(
    client: TestClient,
) -> None:
    """验证 Device APP 主动选择、幂等 claim 和跨设备 rebind。"""

    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:device-claim-rebind"
    )
    first_device_id, first_device_token = register_device(client, token)
    second_device_id, second_device_token = register_device(client, token)

    candidates = client.get(
        "/api/v1/device-sessions/candidates",
        headers=auth_header(first_device_token),
    )
    assert candidates.status_code == 200
    candidate = next(
        item
        for item in candidates.json()["data"]["items"]
        if item["tool_session_id"] == tool_session_id
    )
    assert candidate["tool_type"] == "claude"
    assert candidate["current_device_id"] is None
    assert candidate["controllable"] is True
    assert "workspace_local_path" not in candidates.text
    assert "relay_ticket" not in candidates.text

    first_claim = client.post(
        "/api/v1/device-sessions/claim",
        headers=auth_header(first_device_token),
        json={"tool_session_id": tool_session_id},
    )
    assert first_claim.status_code == 200
    first_device_session_id = first_claim.json()["data"]["id"]

    idempotent_claim = client.post(
        "/api/v1/device-sessions/claim",
        headers=auth_header(first_device_token),
        json={"tool_session_id": tool_session_id},
    )
    assert idempotent_claim.status_code == 200
    assert idempotent_claim.json()["data"]["id"] == first_device_session_id

    second_claim = client.post(
        "/api/v1/device-sessions/claim",
        headers=auth_header(second_device_token),
        json={"tool_session_id": tool_session_id},
    )
    assert second_claim.status_code == 200
    second_device_session_id = second_claim.json()["data"]["id"]
    assert second_device_session_id != first_device_session_id

    async def verify_rebind() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            old = await session.get(DeviceSession, UUID(first_device_session_id))
            current = await session.get(DeviceSession, UUID(second_device_session_id))
            assert old is not None and current is not None
            assert old.status == "stopped"
            assert old.stop_reason == "rebound"
            assert old.device_id == UUID(first_device_id)
            assert current.status == "pending_device"
            assert current.device_id == UUID(second_device_id)
            tool_session = await session.get(Session, UUID(tool_session_id))
            assert tool_session is not None
            assert tool_session.status == "running"
            live = list(
                await session.scalars(
                    select(DeviceSession).where(
                        DeviceSession.tool_session_id == UUID(tool_session_id),
                        DeviceSession.status.in_(
                            {"pending_device", "pending_user_approval", "active"}
                        ),
                    )
                )
            )
            assert [item.id for item in live] == [current.id]
            task_ids = set((await session.scalars(select(NodeTask.task_id))).all())
            assert f"deactivate_device_control:{first_device_session_id}:2" in task_ids

    asyncio.run(verify_rebind())


def test_device_claiming_another_claude_rebounds_its_current_binding(
    client: TestClient,
) -> None:
    """一台设备切换 Claude session 时必须终止旧绑定且不停止任一 Claude。"""

    token = bootstrap(client)
    first_tool_session_id = create_running_tool_session(
        client,
        token,
        project_key="sha256:device-switch-first",
    )
    second_tool_session_id = create_running_tool_session(
        client,
        token,
        project_key="sha256:device-switch-second",
    )
    device_id, device_token = register_device(client, token)
    first = client.post(
        "/api/v1/device-sessions/claim",
        headers=auth_header(device_token),
        json={"tool_session_id": first_tool_session_id},
    )
    assert first.status_code == 200
    second = client.post(
        "/api/v1/device-sessions/claim",
        headers=auth_header(device_token),
        json={"tool_session_id": second_tool_session_id},
    )
    assert second.status_code == 200

    async def verify_switch() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            old = await session.get(DeviceSession, UUID(first.json()["data"]["id"]))
            current = await session.get(DeviceSession, UUID(second.json()["data"]["id"]))
            assert old is not None and current is not None
            assert old.status == "stopped"
            assert old.stop_reason == "rebound"
            assert current.status == "pending_device"
            assert current.device_id == UUID(device_id)
            live_for_device = list(
                await session.scalars(
                    select(DeviceSession).where(
                        DeviceSession.device_id == UUID(device_id),
                        DeviceSession.status.not_in({"stopped", "denied", "expired", "failed"}),
                    )
                )
            )
            assert [item.id for item in live_for_device] == [current.id]
            tool_sessions = list(
                await session.scalars(
                    select(Session).where(
                        Session.id.in_({UUID(first_tool_session_id), UUID(second_tool_session_id)})
                    )
                )
            )
            assert {item.status for item in tool_sessions} == {"running"}

    asyncio.run(verify_switch())


def test_claim_replaces_an_expired_idempotent_binding(client: TestClient) -> None:
    """Expired live rows must not be returned by claim's idempotent fast path."""

    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:expired-idempotent-claim"
    )
    _device_id, device_token = register_device(client, token)
    first = client.post(
        "/api/v1/device-sessions/claim",
        headers=auth_header(device_token),
        json={"tool_session_id": tool_session_id},
    )
    assert first.status_code == 200
    first_id = first.json()["data"]["id"]

    async def expire_first_binding() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            record = await session.get(DeviceSession, UUID(first_id))
            assert record is not None
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire_first_binding())
    replacement = client.post(
        "/api/v1/device-sessions/claim",
        headers=auth_header(device_token),
        json={"tool_session_id": tool_session_id},
    )
    assert replacement.status_code == 200
    assert replacement.json()["data"]["id"] != first_id
    assert replacement.json()["data"]["status"] == "pending_device"

    async def load_old_binding() -> tuple[str, str | None]:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            record = await session.get(DeviceSession, UUID(first_id))
            assert record is not None
            return record.status, record.stop_reason

    assert asyncio.run(load_old_binding()) == ("expired", "session_expired")


def test_candidates_do_not_advertise_expired_current_device(client: TestClient) -> None:
    """Candidate ownership is cleared as soon as its binding TTL has elapsed."""

    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:expired-candidate-binding"
    )
    _device_id, device_token = register_device(client, token)
    claimed = client.post(
        "/api/v1/device-sessions/claim",
        headers=auth_header(device_token),
        json={"tool_session_id": tool_session_id},
    )
    assert claimed.status_code == 200
    binding_id = claimed.json()["data"]["id"]

    async def expire_binding() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            record = await session.get(DeviceSession, UUID(binding_id))
            assert record is not None
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire_binding())
    candidates = client.get(
        "/api/v1/device-sessions/candidates",
        headers=auth_header(device_token),
    )
    assert candidates.status_code == 200
    candidate = next(
        item
        for item in candidates.json()["data"]["items"]
        if item["tool_session_id"] == tool_session_id
    )
    assert candidate["current_device_id"] is None
    assert candidate["device_session_id"] is None


def test_device_session_generation_exhaustion_has_no_partial_mutation(
    client: TestClient,
) -> None:
    """验证代次耗尽在写任务和审计前失败，并保留最终停止代次。"""

    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:generation-boundary"
    )
    device_id, device_token = register_device(client, token)
    created = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    assert created.status_code == 200
    device_session_id = created.json()["data"]["id"]

    async def set_generation(*, status: str) -> tuple[set[str], set[UUID]]:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            record = await session.get(DeviceSession, UUID(device_session_id))
            assert record is not None
            record.generation = MAX_ACTIVE_DEVICE_SESSION_GENERATION
            record.status = status
            record.lease_until = (
                datetime.now(UTC) + timedelta(minutes=1) if status == "active" else None
            )
            await session.commit()
            task_ids = set((await session.scalars(select(NodeTask.task_id))).all())
            audit_ids = set((await session.scalars(select(AuditLog.id))).all())
            return task_ids, audit_ids

    async def load_state() -> tuple[int, str, set[str], set[UUID]]:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            record = await session.get(DeviceSession, UUID(device_session_id))
            assert record is not None
            task_ids = set((await session.scalars(select(NodeTask.task_id))).all())
            audit_ids = set((await session.scalars(select(AuditLog.id))).all())
            return record.generation, record.status, task_ids, audit_ids

    reconnect_tasks, reconnect_audits = asyncio.run(set_generation(status="pending_device"))
    reconnect = client.post(
        f"/api/v1/device-sessions/{device_session_id}/reconnect",
        headers=auth_header(device_token),
        json={"generation": MAX_ACTIVE_DEVICE_SESSION_GENERATION},
    )
    assert reconnect.status_code == 409
    assert reconnect.json()["error"]["code"] == "DEVICE_CONTROL_GENERATION_EXHAUSTED"
    generation, status, task_ids, audit_ids = asyncio.run(load_state())
    assert (generation, status) == (MAX_ACTIVE_DEVICE_SESSION_GENERATION, "pending_device")
    assert task_ids == reconnect_tasks
    assert audit_ids == reconnect_audits

    abort_tasks, abort_audits = asyncio.run(set_generation(status="active"))
    abort = client.post(
        f"/api/v1/device-sessions/{device_session_id}/abort",
        headers=auth_header(device_token),
        json={"generation": MAX_ACTIVE_DEVICE_SESSION_GENERATION, "reason": "esc"},
    )
    assert abort.status_code == 409
    assert abort.json()["error"]["code"] == "DEVICE_CONTROL_GENERATION_EXHAUSTED"
    generation, status, task_ids, audit_ids = asyncio.run(load_state())
    assert (generation, status) == (MAX_ACTIVE_DEVICE_SESSION_GENERATION, "active")
    assert task_ids == abort_tasks
    assert audit_ids == abort_audits

    stopped = client.post(
        f"/api/v1/device-sessions/{device_session_id}/stop",
        headers=auth_header(token),
        json={"reason": "session_end"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["data"]["generation"] == MAX_DEVICE_SESSION_GENERATION
    assert stopped.json()["data"]["status"] == "stopped"
    _, _, task_ids, _ = asyncio.run(load_state())
    assert (
        f"deactivate_device_control:{device_session_id}:{MAX_DEVICE_SESSION_GENERATION}" in task_ids
    )


def test_tool_session_delete_preserves_terminal_device_binding_history(
    client: TestClient,
) -> None:
    """删除远端会话不能绕过 DeviceSession 的独立 retention。"""

    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client,
        token,
        project_key="sha256:preserve-device-binding-history",
    )
    _device_id, device_token = register_device(client, token)
    claimed = client.post(
        "/api/v1/device-sessions/claim",
        headers=auth_header(device_token),
        json={"tool_session_id": tool_session_id},
    )
    assert claimed.status_code == 200
    device_session_id = claimed.json()["data"]["id"]
    stopped = client.post(
        f"/api/v1/device-sessions/{device_session_id}/stop",
        headers=auth_header(device_token),
        json={"reason": "session_end"},
    )
    assert stopped.status_code == 200

    async def mark_tool_session_stopped() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            tool_session = await session.get(Session, UUID(tool_session_id))
            assert tool_session is not None
            tool_session.status = "stopped"
            await session.commit()

    asyncio.run(mark_tool_session_stopped())
    deleted = client.delete(
        f"/api/v1/sessions/{tool_session_id}",
        headers=auth_header(token),
    )
    assert deleted.status_code == 200

    async def load_binding() -> DeviceSession | None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            return await session.get(DeviceSession, UUID(device_session_id))

    history = asyncio.run(load_binding())
    assert history is not None
    assert history.binding_tool_session_id == UUID(tool_session_id)
    assert history.status == "stopped"


def test_device_delete_is_blocked_by_retained_binding_history(client: TestClient) -> None:
    """设备撤销后仍不能通过父表级联删除受 retention 管理的绑定历史。"""

    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client,
        token,
        project_key="sha256:device-delete-binding-history",
    )
    device_id, device_token = register_device(client, token)
    claimed = client.post(
        "/api/v1/device-sessions/claim",
        headers=auth_header(device_token),
        json={"tool_session_id": tool_session_id},
    )
    assert claimed.status_code == 200

    revoked = client.post(
        f"/api/v1/devices/{device_id}/disable",
        headers=auth_header(token),
    )
    assert revoked.status_code == 200
    deleted = client.delete(
        f"/api/v1/devices/{device_id}",
        headers=auth_header(token),
    )
    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "DEVICE_DELETE_BINDING_HISTORY"


def test_device_session_rejects_inactive_tool_session_and_device_token_create(
    client: TestClient,
) -> None:
    token = bootstrap(client)
    create_node(client, token, name="inactive-node", weight=10)
    workspace_device_id, workspace_device_token = register_device(client, token)
    workspace_id = create_workspace(
        client, workspace_device_token, workspace_device_id, "sha256:inactive-control"
    )
    account_id = create_account(client, token)
    tool = client.post(
        "/api/v1/sessions",
        headers=auth_header(token),
        json={
            "tool_type": "claude",
            "tool_account_id": account_id,
            "workspace_id": workspace_id,
            "project_key": "sha256:inactive-control",
            "argv": [],
        },
    )
    assert tool.status_code == 200
    controlled_device_id, controlled_device_token = register_device(client, token)

    inactive = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={
            "device_id": controlled_device_id,
            "tool_session_id": tool.json()["data"]["id"],
        },
    )
    assert inactive.status_code == 409
    assert inactive.json()["error"]["code"] == "DEVICE_CONTROL_TOOL_SESSION_INACTIVE"

    device_token_create = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(controlled_device_token),
        json={
            "device_id": controlled_device_id,
            "tool_session_id": tool.json()["data"]["id"],
        },
    )
    assert device_token_create.status_code == 403


def test_device_session_inbox_is_strictly_scoped_to_authenticated_device(
    client: TestClient,
) -> None:
    token = bootstrap(client)
    first_tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:device-inbox-first"
    )
    second_tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:device-inbox-second"
    )
    first_device_id, first_device_token = register_device(client, token)
    second_device_id, second_device_token = register_device(client, token)

    first = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": first_device_id, "tool_session_id": first_tool_session_id},
    )
    second = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": second_device_id, "tool_session_id": second_tool_session_id},
    )
    assert first.status_code == 200
    assert second.status_code == 200

    first_inbox = client.get(
        "/api/v1/device-sessions/device-inbox",
        headers=auth_header(first_device_token),
    )
    assert first_inbox.status_code == 200
    assert [item["id"] for item in first_inbox.json()["data"]["items"]] == [
        first.json()["data"]["id"]
    ]
    assert "application_digest" not in first_inbox.text
    assert "relay_ticket" not in first_inbox.text

    second_inbox = client.get(
        "/api/v1/device-sessions/device-inbox",
        headers=auth_header(second_device_token),
    )
    assert second_inbox.status_code == 200
    assert [item["id"] for item in second_inbox.json()["data"]["items"]] == [
        second.json()["data"]["id"]
    ]

    user_inbox = client.get("/api/v1/device-sessions/device-inbox", headers=auth_header(token))
    assert user_inbox.status_code == 403


def test_all_denied_approval_never_creates_lease(client: TestClient) -> None:
    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:denied-control"
    )
    device_id, device_token = register_device(client, token)
    created = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    device_session_id = created.json()["data"]["id"]
    assert (
        client.post(
            f"/api/v1/device-sessions/{device_session_id}/device-connected",
            headers=auth_header(device_token),
            json={"generation": 1},
        ).status_code
        == 200
    )
    denied = client.post(
        f"/api/v1/device-sessions/{device_session_id}/approve",
        headers=auth_header(device_token),
        json={
            "generation": 1,
            "approvals": [
                {
                    "application_digest": "b" * 64,
                    "control_level": "view_only",
                    "approval_result": "denied",
                }
            ],
        },
    )
    assert denied.status_code == 200
    assert denied.json()["data"]["status"] == "denied"
    assert denied.json()["data"]["lease_until"] is None


def test_device_session_list_renew_reconnect_and_duplicate_guards(client: TestClient) -> None:
    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:reconnect-control"
    )
    device_id, device_token = register_device(client, token)
    created = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    assert created.status_code == 200
    device_session_id = created.json()["data"]["id"]

    duplicate_create = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    assert duplicate_create.status_code == 409
    assert duplicate_create.json()["error"]["code"] == "DEVICE_CONTROL_SESSION_EXISTS"

    listed = client.get("/api/v1/device-sessions", headers=auth_header(token))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]["items"]] == [device_session_id]
    fetched = client.get(f"/api/v1/device-sessions/{device_session_id}", headers=auth_header(token))
    assert fetched.status_code == 200

    assert (
        client.post(
            f"/api/v1/device-sessions/{device_session_id}/device-connected",
            headers=auth_header(device_token),
            json={"generation": 1},
        ).status_code
        == 200
    )
    duplicate_digest = "c" * 64
    duplicate_approval = client.post(
        f"/api/v1/device-sessions/{device_session_id}/approve",
        headers=auth_header(device_token),
        json={
            "generation": 1,
            "approvals": [
                {
                    "application_digest": duplicate_digest,
                    "control_level": "full_control",
                    "approval_result": "allowed",
                },
                {
                    "application_digest": duplicate_digest,
                    "control_level": "view_only",
                    "approval_result": "denied",
                },
            ],
        },
    )
    assert duplicate_approval.status_code == 422
    assert duplicate_approval.json()["error"]["code"] == "DEVICE_CONTROL_DUPLICATE_APPLICATION"
    approved = client.post(
        f"/api/v1/device-sessions/{device_session_id}/approve",
        headers=auth_header(device_token),
        json={
            "generation": 1,
            "approvals": [
                {
                    "application_digest": duplicate_digest,
                    "control_level": "full_control",
                    "approval_result": "allowed",
                }
            ],
        },
    )
    assert approved.status_code == 200
    first_lease = approved.json()["data"]["lease_until"]
    renewed = client.post(
        f"/api/v1/device-sessions/{device_session_id}/renew",
        headers=auth_header(device_token),
        json={"generation": 1},
    )
    assert renewed.status_code == 200
    assert renewed.json()["data"]["lease_until"] >= first_lease

    first_lock = client.post(
        f"/api/v1/device-sessions/{device_session_id}/lock",
        headers=auth_header(device_token),
        json={"generation": 1},
    )
    assert first_lock.status_code == 200
    repeated_lock = client.post(
        f"/api/v1/device-sessions/{device_session_id}/lock",
        headers=auth_header(device_token),
        json={"generation": 1},
    )
    assert repeated_lock.status_code == 200
    assert repeated_lock.json()["data"]["lock_acquired_at"].removesuffix("Z") == first_lock.json()[
        "data"
    ]["lock_acquired_at"].removesuffix("Z")

    reconnected = client.post(
        f"/api/v1/device-sessions/{device_session_id}/reconnect",
        headers=auth_header(device_token),
        json={"generation": 1},
    )
    assert reconnected.status_code == 200
    reconnect_data = reconnected.json()["data"]
    assert reconnect_data["generation"] == 2
    assert reconnect_data["status"] == "pending_device"
    assert reconnect_data["lease_until"] is None
    assert reconnect_data["lock_acquired_at"] is not None

    stopped = client.post(
        f"/api/v1/device-sessions/{device_session_id}/stop",
        headers=auth_header(device_token),
        json={"reason": "user_stop"},
    )
    assert stopped.status_code == 200
    repeated_stop = client.post(
        f"/api/v1/device-sessions/{device_session_id}/stop",
        headers=auth_header(device_token),
        json={"reason": "user_stop"},
    )
    assert repeated_stop.status_code == 200
    assert repeated_stop.json()["data"]["generation"] == 3


def test_admin_can_list_and_force_stop_other_users_zero_content_sessions(
    client: TestClient,
) -> None:
    """验证管理员只能管理零内容会话元数据且普通用户不能读取全量列表。"""

    admin_token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client, admin_token, project_key="sha256:admin-device-control"
    )
    device_id, _ = register_device(client, admin_token)
    created = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(admin_token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    assert created.status_code == 200
    device_session_id = str(created.json()["data"]["id"])

    user_response = client.post(
        "/api/v1/users",
        headers=auth_header(admin_token),
        json={
            "username": "device-owner",
            "password": "device-owner-secret",
            "display_name": "Device Owner",
            "role": "user",
        },
    )
    assert user_response.status_code == 200
    user_id = UUID(user_response.json()["data"]["id"])
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "device-owner", "password": "device-owner-secret"},
    )
    assert login.status_code == 200
    user_token = str(login.json()["data"]["access_token"])

    legacy_create = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(user_token),
        json={"device_id": str(uuid4()), "tool_session_id": str(uuid4())},
    )
    assert legacy_create.status_code == 403
    assert legacy_create.json()["error"]["code"] == "DEVICE_CONTROL_LEGACY_CREATE_RESTRICTED"

    forbidden_policy = client.get("/api/v1/device-sessions/policy", headers=auth_header(user_token))
    assert forbidden_policy.status_code == 403
    policy = client.get("/api/v1/device-sessions/policy", headers=auth_header(admin_token))
    assert policy.status_code == 200
    assert policy.json()["data"] == {
        "enabled": True,
        "platform": "macos",
        "protocol_version": 1,
        "lease_seconds": 60,
        "maximum_ttl_seconds": 3600,
        "relay_maximum_frame_bytes": 1_048_576,
        "relay_maximum_bytes_per_second": 8_388_608,
        "relay_maximum_connection_seconds": 900,
        "local_approval_required": True,
    }

    async def transfer_session() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            record = await session.get(DeviceSession, UUID(device_session_id))
            assert record is not None
            record.user_id = user_id
            await session.commit()

    asyncio.run(transfer_session())

    forbidden = client.get(
        "/api/v1/device-sessions?all_users=true", headers=auth_header(user_token)
    )
    assert forbidden.status_code == 403

    listed = client.get("/api/v1/device-sessions?all_users=true", headers=auth_header(admin_token))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]["items"]] == [device_session_id]
    assert "application_digest" not in listed.text
    assert "relay_ticket" not in listed.text

    stopped = client.post(
        f"/api/v1/device-sessions/{device_session_id}/stop",
        headers=auth_header(admin_token),
        json={"reason": "user_stop"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["data"]["status"] == "stopped"
    assert stopped.json()["data"]["lock_acquired_at"] is None

    async def read_tool_session_status() -> str:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            tool_session = await session.get(Session, UUID(tool_session_id))
            assert tool_session is not None
            return tool_session.status

    assert asyncio.run(read_tool_session_status()) == "running"

    async def read_admin_audit() -> AuditLog:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            result = await session.scalar(
                select(AuditLog).where(AuditLog.action == "device_session.admin_stop")
            )
            assert result is not None
            return result

    audit = asyncio.run(read_admin_audit())
    assert audit.target_id == device_session_id


def test_device_session_creation_is_disabled_by_default_policy(client: TestClient) -> None:
    """验证部署未显式启用设备控制时拒绝创建会话。"""

    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:disabled-device-control"
    )
    device_id, _ = register_device(client, token)
    app = cast(FastAPI, client.app)
    app.state.settings.device_control_enabled = False

    response = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEVICE_CONTROL_DISABLED"


def test_expired_device_session_is_persisted_and_rejects_connection(client: TestClient) -> None:
    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:expired-control"
    )
    device_id, device_token = register_device(client, token)
    created = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    device_session_id = created.json()["data"]["id"]

    async def expire_record() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            record = await session.get(DeviceSession, UUID(device_session_id))
            assert record is not None
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire_record())
    expired = client.post(
        f"/api/v1/device-sessions/{device_session_id}/device-connected",
        headers=auth_header(device_token),
        json={"generation": 1},
    )
    assert expired.status_code == 409
    assert expired.json()["error"]["code"] == "DEVICE_CONTROL_SESSION_EXPIRED"
    fetched = client.get(f"/api/v1/device-sessions/{device_session_id}", headers=auth_header(token))
    assert fetched.status_code == 200
    assert fetched.json()["data"]["status"] == "expired"


def test_invalid_application_digest_is_rejected_before_service(client: TestClient) -> None:
    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client, token, project_key="sha256:invalid-digest"
    )
    device_id, device_token = register_device(client, token)
    created = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    device_session_id = created.json()["data"]["id"]
    assert (
        client.post(
            f"/api/v1/device-sessions/{device_session_id}/device-connected",
            headers=auth_header(device_token),
            json={"generation": 1},
        ).status_code
        == 200
    )
    invalid = client.post(
        f"/api/v1/device-sessions/{device_session_id}/approve",
        headers=auth_header(device_token),
        json={
            "generation": 1,
            "approvals": [
                {
                    "application_digest": "Z" * 64,
                    "control_level": "view_only",
                    "approval_result": "allowed",
                }
            ],
        },
    )
    assert invalid.status_code == 422


def test_device_relay_material_and_ciphertext_are_role_bound_and_one_time(
    client: TestClient,
) -> None:
    """验证临时连接材料、一次性票据和密文帧中继均严格绑定。"""

    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client,
        token,
        project_key="sha256:relay-control",
    )
    device_id, device_token = register_device(client, token)
    created = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    assert created.status_code == 200
    device_session_id = str(created.json()["data"]["id"])
    node_token = "node_relay_test_token"

    async def authorize_node_token() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            tool_session = await session.get(Session, UUID(tool_session_id))
            assert tool_session is not None
            node = await session.get(Node, tool_session.node_id)
            assert node is not None
            node.node_token_hash = hash_token(app.state.settings.secret_key, node_token)
            node.status = "healthy"
            await session.commit()

    asyncio.run(authorize_node_token())
    device_spki = "1" * 64
    proxy_spki = "2" * 64
    device_waiting = client.post(
        f"/api/v1/device-sessions/{device_session_id}/relay-material",
        headers=auth_header(device_token),
        json={"generation": 1, "spki_sha256": device_spki},
    )
    assert device_waiting.status_code == 200
    assert device_waiting.headers["cache-control"] == "no-store"
    assert device_waiting.json()["data"] == {
        "status": "waiting",
        "role": "device",
        "generation": 1,
        "relay_path": None,
        "relay_ticket": None,
        "peer_spki_sha256": None,
        "exporter_context": None,
        "expires_at": None,
    }

    proxy_ready = client.post(
        f"/api/v1/node-api/device-sessions/{device_session_id}/relay-material",
        headers=auth_header(node_token),
        json={"generation": 1, "spki_sha256": proxy_spki},
    )
    assert proxy_ready.status_code == 200
    proxy_material = proxy_ready.json()["data"]
    assert proxy_material["role"] == "proxy"
    assert proxy_material["peer_spki_sha256"] == device_spki
    assert proxy_material["relay_ticket"].startswith("drelay_")

    device_ready = client.post(
        f"/api/v1/device-sessions/{device_session_id}/relay-material",
        headers=auth_header(device_token),
        json={"generation": 1, "spki_sha256": device_spki},
    )
    assert device_ready.status_code == 200
    device_material = device_ready.json()["data"]
    assert device_material["role"] == "device"
    assert device_material["peer_spki_sha256"] == proxy_spki
    assert device_material["exporter_context"] == proxy_material["exporter_context"]
    assert len(device_material["exporter_context"]) == 64
    assert all(character in "0123456789abcdef" for character in device_material["exporter_context"])
    assert device_material["relay_ticket"] != proxy_material["relay_ticket"]
    assert device_material["relay_path"] == (f"/api/v1/device-sessions/{device_session_id}/relay")

    already_issued = client.post(
        f"/api/v1/device-sessions/{device_session_id}/relay-material",
        headers=auth_header(device_token),
        json={"generation": 1, "spki_sha256": device_spki},
    )
    assert already_issued.status_code == 409
    assert already_issued.json()["error"]["code"] == (
        "DEVICE_CONTROL_RELAY_MATERIAL_ALREADY_ISSUED"
    )

    relay_path = device_material["relay_path"]
    with (
        client.websocket_connect(
            relay_path,
            headers=auth_header(device_material["relay_ticket"]),
        ) as device_socket,
        client.websocket_connect(
            relay_path,
            headers=auth_header(proxy_material["relay_ticket"]),
        ) as proxy_socket,
    ):
        ciphertext = b"opaque-inner-tls-record"
        device_socket.send_bytes(ciphertext)
        assert proxy_socket.receive_bytes() == ciphertext
        proxy_socket.send_bytes(b"opaque-response-record")
        assert device_socket.receive_bytes() == b"opaque-response-record"
        device_socket.send_text("plaintext-is-forbidden")
        with pytest.raises(WebSocketDisconnect) as plaintext:
            device_socket.receive_bytes()
        assert plaintext.value.code == 1003

    with (
        pytest.raises(WebSocketDisconnect) as replayed,
        client.websocket_connect(
            relay_path,
            headers=auth_header(device_material["relay_ticket"]),
        ),
    ):
        pass
    assert replayed.value.code == 1008


def test_device_relay_rejects_wrong_generation_key_change_and_wrong_node(
    client: TestClient,
) -> None:
    """验证错误代次、本代换钥和非绑定 Node 均不能获取中继材料。"""

    token = bootstrap(client)
    tool_session_id = create_running_tool_session(
        client,
        token,
        project_key="sha256:relay-negative",
    )
    device_id, device_token = register_device(client, token)
    created = client.post(
        "/api/v1/device-sessions",
        headers=auth_header(token),
        json={"device_id": device_id, "tool_session_id": tool_session_id},
    )
    device_session_id = str(created.json()["data"]["id"])

    wrong_generation = client.post(
        f"/api/v1/device-sessions/{device_session_id}/relay-material",
        headers=auth_header(device_token),
        json={"generation": 2, "spki_sha256": "3" * 64},
    )
    assert wrong_generation.status_code == 409
    assert wrong_generation.json()["error"]["code"] == "DEVICE_CONTROL_GENERATION_MISMATCH"

    first_key = client.post(
        f"/api/v1/device-sessions/{device_session_id}/relay-material",
        headers=auth_header(device_token),
        json={"generation": 1, "spki_sha256": "3" * 64},
    )
    assert first_key.status_code == 200
    changed_key = client.post(
        f"/api/v1/device-sessions/{device_session_id}/relay-material",
        headers=auth_header(device_token),
        json={"generation": 1, "spki_sha256": "4" * 64},
    )
    assert changed_key.status_code == 409
    assert changed_key.json()["error"]["code"] == "DEVICE_CONTROL_RELAY_KEY_CHANGED"

    _, wrong_node_token = create_node(client, token, name="wrong-relay-node", weight=29)
    wrong_node = client.post(
        f"/api/v1/node-api/device-sessions/{device_session_id}/relay-material",
        headers=auth_header(wrong_node_token),
        json={"generation": 1, "spki_sha256": "5" * 64},
    )
    assert wrong_node.status_code == 404

    bound_node_token = "bound_node_relay_token"

    async def authorize_bound_node_and_later_revoke_device(*, revoke: bool) -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            tool_session = await session.get(Session, UUID(tool_session_id))
            assert tool_session is not None
            node = await session.get(Node, tool_session.node_id)
            assert node is not None
            node.node_token_hash = hash_token(
                app.state.settings.secret_key,
                bound_node_token,
            )
            node.status = "healthy"
            if revoke:
                credential = await session.scalar(
                    select(AuthToken).where(
                        AuthToken.token_hash
                        == hash_token(app.state.settings.secret_key, device_token)
                    )
                )
                assert credential is not None
                credential.status = "revoked"
            await session.commit()

    asyncio.run(authorize_bound_node_and_later_revoke_device(revoke=False))
    proxy_ready = client.post(
        f"/api/v1/node-api/device-sessions/{device_session_id}/relay-material",
        headers=auth_header(bound_node_token),
        json={"generation": 1, "spki_sha256": "6" * 64},
    )
    assert proxy_ready.status_code == 200
    device_ready = client.post(
        f"/api/v1/device-sessions/{device_session_id}/relay-material",
        headers=auth_header(device_token),
        json={"generation": 1, "spki_sha256": "3" * 64},
    )
    assert device_ready.status_code == 200
    asyncio.run(authorize_bound_node_and_later_revoke_device(revoke=True))

    with (
        pytest.raises(WebSocketDisconnect) as revoked,
        client.websocket_connect(
            device_ready.json()["data"]["relay_path"],
            headers=auth_header(device_ready.json()["data"]["relay_ticket"]),
        ),
    ):
        pass
    assert revoked.value.code == 1008


async def test_device_session_service_complete_state_machine() -> None:
    """直接验证异步服务成功链路和跨代 fail-closed 行为。"""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            user = User(
                id=uuid4(),
                username="service-user",
                display_name="Service User",
                role="admin",
                status="active",
                password_hash="hashed",
                totp_enabled=False,
            )
            device = UserDevice(
                id=uuid4(),
                user_id=user.id,
                name="Service Mac",
                platform="macos",
                status="active",
            )
            tool_session = Session(
                id=uuid4(),
                tool_type="claude",
                user_id=user.id,
                tool_account_id=uuid4(),
                workspace_id=uuid4(),
                node_id=uuid4(),
                project_key="sha256:service-state",
                status="running",
                runtime_backend="native",
                device_control_protocol_version=1,
            )
            node = Node(
                id=tool_session.node_id,
                name="service-node",
                status="healthy",
                region_code="US",
                runtime_capabilities={
                    "device_control": {
                        "supported": True,
                        "protocol_versions": [1],
                        "platforms": ["macos"],
                        "backends": ["native"],
                        "capabilities": [
                            "observation_mode_v2",
                            "adaptive_settle_v2",
                            "ax_state_v2",
                        ],
                    }
                },
            )
            session.add_all([user, device, node, tool_session])
            await session.commit()
            token = AuthToken(
                user_id=user.id,
                user_device_id=device.id,
                token_hash="unused-device-token-hash",
                token_type="device",
                status="active",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            user_token = AuthToken(
                user_id=user.id,
                token_hash="unused-user-token-hash",
                token_type="user",
                status="active",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            service = DeviceSessionService(
                session,
                Settings(
                    secret_key="test-secret",
                    device_control_enabled=True,
                ),
            )
            record = await service.create(
                user=user,
                token=user_token,
                device_id=device.id,
                tool_session_id=tool_session.id,
            )
            assert await service.list_for_user(user=user) == [record]
            assert (
                await service.get_for_user(user=user, device_session_id=record.id)
            ).id == record.id
            await service.mark_device_connected(
                token=token, device_session_id=record.id, generation=1
            )
            approval = DeviceApprovalItem(
                application_digest="d" * 64,
                control_level="full_control",
                approval_result="allowed",
                clipboard_allowed=False,
            )
            await service.approve(
                token=token,
                device_session_id=record.id,
                generation=1,
                approvals=[approval],
            )
            context_task = await session.scalar(
                select(NodeTask).where(
                    NodeTask.task_type == "update_device_control_context",
                    NodeTask.payload["generation"].as_integer() == 1,
                )
            )
            assert context_task is not None
            assert context_task.payload["capabilities"] == [
                "adaptive_settle_v2",
                "ax_state_v2",
                "observation_mode_v2",
            ]
            locked = await service.acquire_lock(
                token=token, device_session_id=record.id, generation=1
            )
            lock_time = locked.lock_acquired_at
            assert lock_time is not None
            assert (
                await service.acquire_lock(token=token, device_session_id=record.id, generation=1)
            ).lock_acquired_at == lock_time
            await service.renew(token=token, device_session_id=record.id, generation=1)
            reconnected = await service.reconnect(
                token=token, device_session_id=record.id, generation=1
            )
            assert reconnected.generation == 2
            assert reconnected.lock_acquired_at == lock_time
            await service.mark_device_connected(
                token=token, device_session_id=record.id, generation=2
            )
            await service.approve(
                token=token,
                device_session_id=record.id,
                generation=2,
                approvals=[approval],
            )
            aborted = await service.abort_action(
                token=token,
                device_session_id=record.id,
                generation=2,
                reason="esc",
            )
            assert aborted.generation == 3
            assert aborted.lock_acquired_at == lock_time
            stopped = await service.stop_by_user(
                user=user, device_session_id=record.id, reason="session_end"
            )
            assert stopped.status == "stopped"
            assert stopped.lock_acquired_at is None
            assert (
                await service.stop_by_device(
                    token=token, device_session_id=record.id, reason="user_stop"
                )
            ).generation == 4
    finally:
        await engine.dispose()


async def test_device_session_service_rejects_wrong_binding_and_expired_lease() -> None:
    """直接验证错误设备、状态、代次和过期租约均默认拒绝。"""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            user = User(
                id=uuid4(),
                username="negative-user",
                display_name="Negative User",
                role="admin",
                status="active",
                password_hash="hashed",
                totp_enabled=False,
            )
            device = UserDevice(
                id=uuid4(),
                user_id=user.id,
                name="Negative Mac",
                platform="macos",
                status="active",
            )
            tool_session = Session(
                id=uuid4(),
                tool_type="claude",
                user_id=user.id,
                tool_account_id=uuid4(),
                workspace_id=uuid4(),
                node_id=uuid4(),
                project_key="sha256:negative-state",
                status="running",
                runtime_backend="native",
                device_control_protocol_version=1,
            )
            node = Node(
                id=tool_session.node_id,
                name="negative-node",
                status="healthy",
                region_code="US",
                runtime_capabilities={
                    "device_control": {
                        "supported": True,
                        "protocol_versions": [1],
                        "platforms": ["macos"],
                        "backends": ["native"],
                    }
                },
            )
            session.add_all([user, device, node, tool_session])
            await session.commit()
            user_token = AuthToken(
                user_id=user.id,
                token_hash="negative-user-token",
                token_type="user",
                status="active",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            device_token = AuthToken(
                user_id=user.id,
                user_device_id=device.id,
                token_hash="negative-device-token",
                token_type="device",
                status="active",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            wrong_device_token = AuthToken(
                user_id=user.id,
                user_device_id=uuid4(),
                token_hash="wrong-device-token",
                token_type="device",
                status="active",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            service = DeviceSessionService(
                session, Settings(secret_key="test-secret", device_control_enabled=True)
            )
            record = await service.create(
                user=user,
                token=user_token,
                device_id=device.id,
                tool_session_id=tool_session.id,
            )
            with pytest.raises(ApiError) as wrong_binding:
                await service.mark_device_connected(
                    token=wrong_device_token,
                    device_session_id=record.id,
                    generation=1,
                )
            assert wrong_binding.value.status_code == 404
            with pytest.raises(ApiError) as wrong_generation:
                await service.mark_device_connected(
                    token=device_token,
                    device_session_id=record.id,
                    generation=2,
                )
            assert wrong_generation.value.code == "DEVICE_CONTROL_GENERATION_MISMATCH"
            await service.mark_device_connected(
                token=device_token, device_session_id=record.id, generation=1
            )
            with pytest.raises(ApiError) as state_conflict:
                await service.mark_device_connected(
                    token=device_token, device_session_id=record.id, generation=1
                )
            assert state_conflict.value.code == "DEVICE_CONTROL_STATE_CONFLICT"
            await service.approve(
                token=device_token,
                device_session_id=record.id,
                generation=1,
                approvals=[
                    DeviceApprovalItem(
                        application_digest="e" * 64,
                        control_level="view_only",
                        approval_result="allowed",
                    )
                ],
            )
            record.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            with pytest.raises(ApiError) as lease_expired:
                await service.renew(token=device_token, device_session_id=record.id, generation=1)
            assert lease_expired.value.code == "DEVICE_CONTROL_LEASE_EXPIRED"
            refreshed = await session.get(DeviceSession, record.id)
            assert refreshed is not None
            assert refreshed.status == "expired"
            assert refreshed.lock_acquired_at is None
    finally:
        await engine.dispose()
