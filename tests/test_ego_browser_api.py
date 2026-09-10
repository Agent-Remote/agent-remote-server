import asyncio
import base64
import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_sessions_api import (
    auth_header,
    bootstrap,
    create_account,
    create_node,
    create_workspace,
    register_device,
)

from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.ego_browser.relay import (
    EgoBrowserRelayTicketClaims,
    InMemoryEgoBrowserRelayStore,
)
from agent_remote_server.main import create_app
from agent_remote_server.models import (
    AuditLog,
    EgoBrowserBinding,
    EgoBrowserRequestLedger,
    EgoBrowserRevocationOutbox,
    Node,
    NodeTask,
    NodeTaskResult,
    Session,
)
from agent_remote_server.security import hash_token
from agent_remote_server.services.ego_browser import REQUIRED_CAPABILITIES, EgoBrowserService

_ENCRYPTION_PUBLIC_KEY = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"
_ROTATED_SIGNING_PUBLIC_KEY = "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
_ROTATED_ENCRYPTION_PUBLIC_KEY = "AwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwM"


async def create_schema(app: FastAPI) -> None:
    """创建隔离的 API 测试 schema。"""

    async with app.state.database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """创建已启用开发环境 browser 能力的应用。"""

    settings = Settings(
        secret_key="test-secret",
        log_level="CRITICAL",
        database_url="sqlite+aiosqlite:///:memory:",
        node_task_lease_seconds=30,
        node_offline_after_seconds=60,
        ego_browser_bridge_enabled=True,
        ego_browser_require_device_pop=False,
    )
    app = create_app(settings)
    asyncio.run(create_schema(app))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def pop_client() -> Iterator[TestClient]:
    """创建强制启用设备 PoP 的隔离应用。"""

    settings = Settings(
        secret_key="test-pop-secret",
        log_level="CRITICAL",
        database_url="sqlite+aiosqlite:///:memory:",
        public_base_url="https://control.example.test",
        ego_browser_bridge_enabled=True,
        ego_browser_require_device_pop=True,
    )
    app = create_app(settings)
    asyncio.run(create_schema(app))
    with TestClient(app) as test_client:
        yield test_client


def create_user(client: TestClient, admin_token: str, username: str) -> tuple[str, str]:
    """创建普通用户并返回其 ID 和访问 token。"""

    password = f"{username}-secret"
    created = client.post(
        "/api/v1/users",
        headers=auth_header(admin_token),
        json={
            "username": username,
            "password": password,
            "display_name": username.title(),
            "role": "user",
        },
    )
    assert created.status_code == 200
    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert logged_in.status_code == 200
    return str(created.json()["data"]["id"]), str(logged_in.json()["data"]["access_token"])


def create_running_session(
    client: TestClient,
    *,
    admin_token: str,
    owner_token: str,
) -> tuple[str, str, str]:
    """在兼容 Node 上创建运行中的 Claude session。"""

    node_id, node_token = create_node(
        client,
        admin_token,
        name="ego-browser-node",
        weight=37,
    )
    workspace_device_id, workspace_device_token = register_device(client, owner_token)
    workspace_id = create_workspace(
        client,
        workspace_device_token,
        workspace_device_id,
        "sha256:ego-browser-api",
    )
    account_id = create_account(client, owner_token)
    created = client.post(
        "/api/v1/sessions",
        headers=auth_header(owner_token),
        json={
            "tool_type": "claude",
            "tool_account_id": account_id,
            "workspace_id": workspace_id,
            "project_key": "sha256:ego-browser-api",
            "argv": [],
        },
    )
    assert created.status_code == 200
    tool_session_id = str(created.json()["data"]["id"])

    async def mark_compatible() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            tool_session = await session.get(Session, UUID(tool_session_id))
            node = await session.get(Node, UUID(node_id))
            assert tool_session is not None and node is not None
            tool_session.status = "running"
            tool_session.runtime_backend = "native"
            node.status = "healthy"
            node.runtime_capabilities = {
                "ego_browser_bridge": {
                    "supported": True,
                    "protocol_versions": ["ego-browser-bridge-v1"],
                    "backends": ["native", "docker_sandbox"],
                    "wrapper_version": "0.1.11",
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
            await session.commit()

    asyncio.run(mark_compatible())
    return tool_session_id, node_id, node_token


def ego_browser_device_payload(device_id: str) -> dict[str, object]:
    """构造规范的逻辑测试 Device Client 注册 payload。"""

    return {
        "device_id": device_id,
        "public_key": "A" * 43,
        "encryption_public_key": _ENCRYPTION_PUBLIC_KEY,
        "generation": 1,
        "release_profile": "logic-test",
        "credential_profile": "community_file",
        "platform": "macos",
        "bridge_protocol_version": "ego-browser-bridge-v1",
        "bridge_version": "0.1.11",
        "local_ego_browser_runtime_version": "1.2.3",
        "ego_lite_runtime_version": "1.2.3",
        "skill_version": "1.2.3",
        "capabilities": list(REQUIRED_CAPABILITIES),
        "allowlist_revision": 1,
    }


def signed_pop_payload(
    *,
    private_key: Ed25519PrivateKey,
    payload: dict[str, object],
    challenge: str,
    operation: str,
    device_id: str,
    device_generation: int,
    operation_generation: int,
    binding_id: str | None = None,
    release_profile: str = "logic-test",
    credential_profile: str = "community_file",
) -> dict[str, object]:
    """使用共享 v2 PoP transcript 签署 API payload。"""

    def field(value: str) -> bytes:
        encoded = value.encode()
        return len(encoded).to_bytes(4, "big") + encoded

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    challenge_bytes = base64.urlsafe_b64decode(challenge + "=" * (-len(challenge) % 4))
    message = (
        b"agent-remote/ego-browser/pop/v2\0"
        + field(operation)
        + field(device_id)
        + device_generation.to_bytes(8, "big")
        + operation_generation.to_bytes(8, "big")
        + field(binding_id or "")
        + field(release_profile)
        + field(credential_profile)
        + field("control.example.test")
        + challenge_bytes
        + hashlib.sha256(canonical).digest()
    )
    signed = dict(payload)
    signed["proof_challenge"] = challenge
    signed["proof_signature"] = (
        base64.urlsafe_b64encode(private_key.sign(message)).decode().rstrip("=")
    )
    return signed


def test_device_pop_binds_payload_and_consumes_challenge_once(pop_client: TestClient) -> None:
    """篡改或重放的设备请求必须在改变状态前失败。"""

    admin_token = bootstrap(pop_client)
    _, owner_token = create_user(pop_client, admin_token, "browser-pop-owner")
    device_id = str(uuid4())
    private_key = Ed25519PrivateKey.from_private_bytes(bytes([11]) * 32)
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    payload = ego_browser_device_payload(device_id)
    payload["public_key"] = base64.urlsafe_b64encode(public_key).decode().rstrip("=")

    challenge_response = pop_client.post(
        "/api/v1/ego-browser/proof-challenges",
        headers=auth_header(owner_token),
        json={
            "operation": "register_device",
            "ego_browser_device_id": device_id,
            "generation": 1,
            "binding_id": None,
        },
    )
    assert challenge_response.status_code == 200
    assert challenge_response.headers["cache-control"] == "no-store"
    signed = signed_pop_payload(
        private_key=private_key,
        payload=payload,
        challenge=str(challenge_response.json()["data"]["challenge"]),
        operation="register_device",
        device_id=device_id,
        device_generation=1,
        operation_generation=1,
    )

    tampered = dict(signed)
    tampered["bridge_version"] = "0.1.1"
    rejected = pop_client.post(
        "/api/v1/ego-browser/devices/register",
        headers=auth_header(owner_token),
        json=tampered,
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "EGO_BROWSER_POP_INVALID"

    registered = pop_client.post(
        "/api/v1/ego-browser/devices/register",
        headers=auth_header(owner_token),
        json=signed,
    )
    assert registered.status_code == 200, registered.text

    replayed = pop_client.post(
        "/api/v1/ego-browser/devices/register",
        headers=auth_header(owner_token),
        json=signed,
    )
    assert replayed.status_code == 403
    assert replayed.json()["error"]["code"] == "EGO_BROWSER_POP_CHALLENGE_INVALID"

    device_token = str(registered.json()["data"]["credential"]["access_token"])
    revoke_payload: dict[str, object] = {"generation": 1, "reason": "device_revoked"}
    revoke_challenge = pop_client.post(
        "/api/v1/ego-browser/proof-challenges",
        headers=auth_header(device_token),
        json={
            "operation": "revoke_device",
            "ego_browser_device_id": device_id,
            "generation": 1,
            "binding_id": None,
        },
    )
    assert revoke_challenge.status_code == 200
    signed_revoke = signed_pop_payload(
        private_key=private_key,
        payload=revoke_payload,
        challenge=str(revoke_challenge.json()["data"]["challenge"]),
        operation="revoke_device",
        device_id=device_id,
        device_generation=1,
        operation_generation=1,
    )
    revoked = pop_client.post(
        f"/api/v1/ego-browser/devices/{device_id}/revoke",
        headers=auth_header(device_token),
        json=signed_revoke,
    )
    assert (revoked.status_code, revoked.json()["data"]["status"]) == (200, "revoked")
    rejected_credential = pop_client.get(
        "/api/v1/ego-browser/devices",
        headers=auth_header(device_token),
    )
    assert rejected_credential.status_code == 401
    assert rejected_credential.json()["error"]["code"] == "EGO_BROWSER_CREDENTIAL_REVOKED"


def register_ego_browser_device(client: TestClient, owner_token: str) -> tuple[str, str]:
    """注册独立 browser 设备并返回一次性凭据。"""

    device_id = str(uuid4())
    registered = client.post(
        "/api/v1/ego-browser/devices/register",
        headers=auth_header(owner_token),
        json=ego_browser_device_payload(device_id),
    )
    assert registered.status_code == 200, registered.text
    assert registered.headers["cache-control"] == "no-store"
    credential = registered.json()["data"]["credential"]
    assert credential["access_token"].startswith("egbc_")
    return device_id, str(credential["access_token"])


def claim_binding(
    client: TestClient,
    *,
    device_id: str,
    device_token: str,
    tool_session_id: str,
) -> str:
    """通过显式全信任确认认领运行中的 session。"""

    claimed = client.post(
        "/api/v1/ego-browser/bindings/claim",
        headers=auth_header(device_token),
        json=ego_browser_claim_payload(device_id, tool_session_id),
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["status"] == "pending_device"
    assert claimed.json()["data"]["task_space_label"] == f"agent-remote:{tool_session_id}"
    return str(claimed.json()["data"]["id"])


def ego_browser_claim_payload(device_id: str, tool_session_id: str) -> dict[str, object]:
    """构造显式全信任 binding claim payload。"""

    return {
        "tool_session_id": tool_session_id,
        "ego_browser_device_id": device_id,
        "encryption_public_key": _ENCRYPTION_PUBLIC_KEY,
        "authorization_mode": "ego_browser_script_full_trust",
        "authorization_policy_version": 1,
        "release_profile": "logic-test",
        "credential_profile": "community_file",
        "remote_platform": "linux",
        "local_platform": "macos",
        "device_capabilities": list(REQUIRED_CAPABILITIES),
        "allowlist_revision": 1,
        "task_space_label": f"agent-remote:{tool_session_id}",
        "concurrency_mode": "task_space_tab",
        "user_confirmation": True,
    }


def test_docker_session_can_be_claimed_when_node_advertises_backend(client: TestClient) -> None:
    """Node 明确上报 Docker 支持后应允许对应会话建立浏览器绑定。"""

    admin_token = bootstrap(client)
    _, owner_token = create_user(client, admin_token, "browser-docker-owner")
    tool_session_id, _, _ = create_running_session(
        client,
        admin_token=admin_token,
        owner_token=owner_token,
    )

    async def select_docker_backend() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            tool_session = await session.get(Session, UUID(tool_session_id))
            assert tool_session is not None
            tool_session.runtime_backend = "docker_sandbox"
            await session.commit()

    asyncio.run(select_docker_backend())
    device_id, device_token = register_ego_browser_device(client, owner_token)
    candidates = client.get(
        "/api/v1/ego-browser/bindings/candidates",
        headers=auth_header(device_token),
    )
    assert candidates.status_code == 200
    candidate = next(
        item
        for item in candidates.json()["data"]["items"]
        if item["tool_session_id"] == tool_session_id
    )
    assert candidate["runtime_backend"] == "docker_sandbox"
    assert candidate["controllable"] is True

    claimed = client.post(
        "/api/v1/ego-browser/bindings/claim",
        headers=auth_header(device_token),
        json=ego_browser_claim_payload(device_id, tool_session_id),
    )
    assert claimed.status_code == 200
    assert claimed.json()["data"]["tool_session_id"] == tool_session_id


def connect_binding(
    client: TestClient,
    *,
    binding_id: str,
    device_token: str,
    generation: int,
    allowlist_revision: int = 1,
    allowlist_roots_digest: str | None = None,
) -> None:
    """使用匹配的能力快照激活已认领 generation。"""

    capabilities = list(REQUIRED_CAPABILITIES)
    if allowlist_roots_digest is not None:
        capabilities.append("ego_browser_file_allowlist_v1")
    connected = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/connected",
        headers=auth_header(device_token),
        json={
            "generation": generation,
            "encryption_public_key": _ENCRYPTION_PUBLIC_KEY,
            "bridge_protocol_version": "ego-browser-bridge-v1",
            "bridge_version": "0.1.11",
            "local_ego_browser_runtime_version": "1.2.3",
            "ego_lite_runtime_version": "1.2.3",
            "skill_version": "1.2.3",
            "release_profile": "logic-test",
            "signer_certificate_sha256": "development",
            "credential_profile": "community_file",
            "allowlist_revision": allowlist_revision,
            "allowlist_roots_digest": allowlist_roots_digest,
            "max_parallel_requests": 4,
            "capabilities": capabilities,
            "local_browser_ready": True,
        },
    )
    assert connected.status_code == 200, connected.text
    assert connected.json()["data"]["status"] == "active"


def create_active_binding(
    client: TestClient, *, username: str
) -> tuple[str, str, str, str, str, str, str]:
    """创建 active binding 并返回其所有者、Node 和设备身份。"""

    admin_token = bootstrap(client)
    owner_id, owner_token = create_user(client, admin_token, username)
    tool_session_id, node_id, node_token = create_running_session(
        client,
        admin_token=admin_token,
        owner_token=owner_token,
    )
    device_id, device_token = register_ego_browser_device(client, owner_token)
    binding_id = claim_binding(
        client,
        device_id=device_id,
        device_token=device_token,
        tool_session_id=tool_session_id,
    )
    connect_binding(
        client,
        binding_id=binding_id,
        device_token=device_token,
        generation=1,
    )
    return admin_token, owner_id, owner_token, node_id, node_token, device_id, binding_id


def test_owner_device_revoke_invalidates_live_binding_and_credential(client: TestClient) -> None:
    """所有者恢复撤销必须原子终止 binding 和设备凭据。"""

    admin_token, _, owner_token, _, _, device_id, binding_id = create_active_binding(
        client, username="browser-device-revoke-owner"
    )
    device_token = str(
        client.post(
            "/api/v1/ego-browser/devices/register",
            headers=auth_header(owner_token),
            json=ego_browser_device_payload(device_id),
        ).json()["data"]["credential"]["access_token"]
    )

    revoked = client.post(
        f"/api/v1/ego-browser/devices/{device_id}/revoke",
        headers=auth_header(owner_token),
        json={"generation": 1, "reason": "device_revoked"},
    )
    assert (revoked.status_code, revoked.json()["data"]["status"]) == (200, "revoked")
    binding = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}",
        headers=auth_header(admin_token),
    )
    assert (
        binding.json()["data"]["status"],
        binding.json()["data"]["generation"],
        binding.json()["data"]["stop_reason"],
    ) == ("revoked", 2, "device_revoked")
    stale_credential = client.get(
        "/api/v1/ego-browser/devices",
        headers=auth_header(device_token),
    )
    assert stale_credential.status_code == 401
    assert stale_credential.json()["error"]["code"] == "EGO_BROWSER_CREDENTIAL_REVOKED"


@pytest.mark.parametrize("endpoint", ["patch", "disable"])
def test_user_disable_revokes_live_ego_browser_binding(client: TestClient, endpoint: str) -> None:
    """所有管理员禁用用户路径都必须立即撤销 browser 权限。"""

    admin_token, owner_id, _, _, _, _, binding_id = create_active_binding(
        client, username=f"browser-user-{endpoint}"
    )
    if endpoint == "patch":
        response = client.patch(
            f"/api/v1/users/{owner_id}",
            headers=auth_header(admin_token),
            json={"status": "disabled"},
        )
    else:
        response = client.post(
            f"/api/v1/users/{owner_id}/disable",
            headers=auth_header(admin_token),
        )
    assert response.status_code == 200

    binding = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}",
        headers=auth_header(admin_token),
    ).json()["data"]
    assert (binding["status"], binding["generation"], binding["stop_reason"]) == (
        "revoked",
        2,
        "user_disabled",
    )


@pytest.mark.parametrize("trigger", ["disable", "heartbeat_loss"])
def test_node_loss_revokes_live_ego_browser_binding(client: TestClient, trigger: str) -> None:
    """显式禁用 Node 和心跳过期都必须撤销 browser binding。"""

    admin_token, _, _, node_id, _, _, binding_id = create_active_binding(
        client, username=f"browser-node-{trigger}"
    )
    if trigger == "disable":
        response = client.post(
            f"/api/v1/nodes/{node_id}/disable",
            headers=auth_header(admin_token),
        )
    else:

        async def make_stale() -> None:
            app = cast(FastAPI, client.app)
            async with app.state.session_factory() as session:
                node = await session.get(Node, UUID(node_id))
                assert node is not None
                node.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
                await session.commit()

        asyncio.run(make_stale())
        response = client.get(
            f"/api/v1/nodes/{node_id}",
            headers=auth_header(admin_token),
        )
    assert response.status_code == 200
    assert response.json()["data"]["status"] in {"disabled", "offline"}

    binding = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}",
        headers=auth_header(admin_token),
    ).json()["data"]
    expected_reason = "node_unavailable" if trigger == "disable" else "node_heartbeat_lost"
    assert (binding["status"], binding["generation"], binding["stop_reason"]) == (
        "revoked",
        2,
        expected_reason,
    )


@pytest.mark.parametrize("reader", ["bindings", "requests", "node_bindings"])
def test_expiry_triggering_reads_publish_revocation_immediately(
    client: TestClient,
    reader: str,
) -> None:
    """读取路径触发租约过期时必须在响应前投递 durable revocation。"""

    _, _, owner_token, _, node_token, _, binding_id = create_active_binding(
        client,
        username=f"browser-expiry-reader-{reader}",
    )
    app = cast(FastAPI, client.app)

    async def expire_lease() -> None:
        async with app.state.session_factory() as session:
            binding = await session.get(EgoBrowserBinding, UUID(binding_id))
            assert binding is not None
            binding.lease_until = datetime.now(UTC) - timedelta(seconds=11)
            await session.commit()

    asyncio.run(expire_lease())
    if reader == "bindings":
        response = client.get(
            "/api/v1/ego-browser/bindings",
            headers=auth_header(owner_token),
        )
    elif reader == "requests":
        response = client.get(
            f"/api/v1/ego-browser/bindings/{binding_id}/requests",
            headers=auth_header(owner_token),
        )
    else:
        response = client.get(
            "/api/v1/node-api/ego-browser/bindings",
            headers=auth_header(node_token),
        )
    assert response.status_code == 200, response.text

    async def verify_delivery() -> None:
        async with app.state.session_factory() as session:
            binding = await session.get(EgoBrowserBinding, UUID(binding_id))
            event = await session.scalar(
                select(EgoBrowserRevocationOutbox).where(
                    EgoBrowserRevocationOutbox.binding_id == UUID(binding_id)
                )
            )
            assert binding is not None
            assert (binding.status, binding.generation) == ("expired", 2)
            assert event is not None
            assert event.reason == "renewal_grace_expired"
            assert event.delivered_at is not None

    asyncio.run(verify_delivery())


def test_http_lifecycle_preserves_device_only_authorization_and_admin_cleanup(
    client: TestClient,
) -> None:
    """验证用户、Device Client 与 Node 的完整控制面合同。"""

    admin_token = bootstrap(client)
    owner_id, owner_token = create_user(client, admin_token, "browser-owner")
    _, stranger_token = create_user(client, admin_token, "browser-stranger")
    tool_session_id, node_id, node_token = create_running_session(
        client,
        admin_token=admin_token,
        owner_token=owner_token,
    )
    device_id, device_token = register_ego_browser_device(client, owner_token)

    policy = client.get("/api/v1/ego-browser/policy", headers=auth_header(owner_token))
    assert policy.status_code == 200
    assert policy.json()["data"]["authorization_mode"] == "ego_browser_script_full_trust"

    devices = client.get("/api/v1/ego-browser/devices", headers=auth_header(device_token))
    assert devices.status_code == 200
    assert [item["id"] for item in devices.json()["data"]["items"]] == [device_id]
    assert "access_token" not in devices.text

    candidates = client.get(
        "/api/v1/ego-browser/bindings/candidates",
        headers=auth_header(device_token),
    )
    assert candidates.status_code == 200
    candidate = next(
        item
        for item in candidates.json()["data"]["items"]
        if item["tool_session_id"] == tool_session_id
    )
    assert candidate["controllable"] is True
    assert candidate["node_id"] == node_id

    mismatched_task_space = ego_browser_claim_payload(device_id, tool_session_id)
    mismatched_task_space["task_space_label"] = "agent-remote:another-session"
    rejected_claim = client.post(
        "/api/v1/ego-browser/bindings/claim",
        headers=auth_header(device_token),
        json=mismatched_task_space,
    )
    assert rejected_claim.status_code == 422
    assert rejected_claim.json()["error"]["code"] == "EGO_BROWSER_INVALID_TASK_SPACE"

    binding_id = claim_binding(
        client,
        device_id=device_id,
        device_token=device_token,
        tool_session_id=tool_session_id,
    )
    connect_binding(
        client,
        binding_id=binding_id,
        device_token=device_token,
        generation=1,
    )

    owner_binding = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}",
        headers=auth_header(owner_token),
    )
    assert owner_binding.status_code == 200
    assert owner_binding.json()["data"]["encryption_public_key"] == _ENCRYPTION_PUBLIC_KEY
    assert owner_binding.json()["data"]["user_id"] == owner_id
    assert "relay_ticket" not in owner_binding.text

    node_bindings = client.get(
        "/api/v1/node-api/ego-browser/bindings",
        headers=auth_header(node_token),
    )
    assert node_bindings.status_code == 200
    assert [item["binding_id"] for item in node_bindings.json()["data"]["items"]] == [binding_id]

    device_renewed = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/renew",
        headers=auth_header(device_token),
        json={"generation": 1, "allowlist_revision": 1},
    )
    assert device_renewed.status_code == 200
    node_renewed = client.post(
        f"/api/v1/node-api/ego-browser/bindings/{binding_id}/renew",
        headers=auth_header(node_token),
        json={"generation": 1, "allowlist_revision": 1},
    )
    assert node_renewed.status_code == 200
    assert node_renewed.json()["data"]["lease_health"] == "healthy"

    bridge_ticket = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/relay-ticket",
        headers=auth_header(device_token),
        json={
            "generation": 1,
            "role": "bridge",
            "ego_browser_device_id": device_id,
        },
    )
    assert bridge_ticket.status_code == 200
    assert bridge_ticket.headers["cache-control"] == "no-store"
    assert bridge_ticket.json()["data"]["relay_ticket"].startswith("egbr_")
    wrapper_ticket = client.post(
        f"/api/v1/node-api/ego-browser/bindings/{binding_id}/relay-ticket",
        headers=auth_header(node_token),
        json={"generation": 1, "role": "wrapper"},
    )
    assert wrapper_ticket.status_code == 200
    assert wrapper_ticket.json()["data"]["role"] == "wrapper"

    allowlist = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}/allowlist",
        headers=auth_header(owner_token),
    )
    assert allowlist.status_code == 200
    assert allowlist.json()["data"]["file_limits"] == {
        "max_file_bytes": 64 * 1024 * 1024,
        "max_total_bytes": 256 * 1024 * 1024,
        "max_file_count": 32,
    }

    roots_digest = f"sha256:{'c' * 64}"
    updated = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/allowlist/confirm",
        headers=auth_header(device_token),
        json={
            "generation": 1,
            "expected_revision": 1,
            "roots_digest": roots_digest,
            "user_confirmation": True,
        },
    )
    assert updated.status_code == 200
    assert (updated.json()["data"]["status"], updated.json()["data"]["generation"]) == (
        "paused",
        2,
    )
    assert updated.json()["data"]["lease_grace_until"] is None

    user_resume = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/resume",
        headers=auth_header(owner_token),
        json={
            "generation": 2,
            "user_confirmation": True,
            "allowlist_revision": 2,
        },
    )
    assert user_resume.status_code == 401
    assert user_resume.json()["error"]["code"] == "EGO_BROWSER_CREDENTIAL_INVALID"

    resumed = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/resume",
        headers=auth_header(device_token),
        json={
            "generation": 2,
            "user_confirmation": True,
            "allowlist_revision": 2,
        },
    )
    assert resumed.status_code == 200
    assert (resumed.json()["data"]["status"], resumed.json()["data"]["generation"]) == (
        "connecting",
        3,
    )
    connect_binding(
        client,
        binding_id=binding_id,
        device_token=device_token,
        generation=3,
        allowlist_revision=2,
        allowlist_roots_digest=roots_digest,
    )

    stranger_get = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}",
        headers=auth_header(stranger_token),
    )
    assert stranger_get.status_code == 404
    forbidden_all = client.get(
        "/api/v1/ego-browser/bindings?all_users=true",
        headers=auth_header(stranger_token),
    )
    assert forbidden_all.status_code == 403

    paused = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/pause",
        headers=auth_header(owner_token),
        json={"generation": 3, "reason": "user_pause"},
    )
    assert (paused.status_code, paused.json()["data"]["generation"]) == (200, 4)
    assert paused.json()["data"]["stop_reason"] == "user_pause"

    resumed = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/resume",
        headers=auth_header(device_token),
        json={
            "generation": 4,
            "user_confirmation": True,
            "allowlist_revision": 2,
        },
    )
    assert resumed.status_code == 200
    assert resumed.json()["data"]["generation"] == 5
    connect_binding(
        client,
        binding_id=binding_id,
        device_token=device_token,
        generation=5,
        allowlist_revision=2,
        allowlist_roots_digest=roots_digest,
    )

    admin_devices = client.get(
        "/api/v1/ego-browser/devices?all_users=true",
        headers=auth_header(admin_token),
    )
    admin_bindings = client.get(
        "/api/v1/ego-browser/bindings?all_users=true",
        headers=auth_header(admin_token),
    )
    assert [item["id"] for item in admin_devices.json()["data"]["items"]] == [device_id]
    assert [item["id"] for item in admin_bindings.json()["data"]["items"]] == [binding_id]

    stopped = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/stop",
        headers=auth_header(admin_token),
        json={"generation": 5, "reason": "admin_cleanup"},
    )
    assert (stopped.status_code, stopped.json()["data"]["status"]) == (200, "stopped")
    stopped_generation = stopped.json()["data"]["generation"]

    rejected_allowlist = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/allowlist/confirm",
        headers=auth_header(device_token),
        json={
            "generation": stopped_generation,
            "expected_revision": 2,
            "roots_digest": f"sha256:{'e' * 64}",
            "user_confirmation": True,
        },
    )
    assert rejected_allowlist.status_code == 409
    assert rejected_allowlist.json()["error"]["code"] == "EGO_BROWSER_STATE_CONFLICT"
    unchanged_allowlist = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}/allowlist",
        headers=auth_header(owner_token),
    ).json()["data"]
    assert unchanged_allowlist == {
        "allowlist_revision": 2,
        "roots_digest": roots_digest,
        "file_limits": {
            "max_file_bytes": 64 * 1024 * 1024,
            "max_total_bytes": 256 * 1024 * 1024,
            "max_file_count": 32,
        },
    }

    revoked = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/revoke",
        headers=auth_header(admin_token),
        json={"generation": stopped_generation, "reason": "admin_revoke"},
    )
    assert (revoked.status_code, revoked.json()["data"]["status"]) == (200, "revoked")
    assert revoked.json()["data"]["generation"] == stopped_generation + 1
    assert revoked.json()["data"]["stop_reason"] == "admin_revoke"


def test_device_policy_and_key_rotation_revoke_old_credentials_and_generations(
    client: TestClient,
) -> None:
    """策略和密钥轮换必须使此前的全部执行权限失效。"""

    admin_token = bootstrap(client)
    _, owner_token = create_user(client, admin_token, "rotation-owner")
    tool_session_id, _, _ = create_running_session(
        client,
        admin_token=admin_token,
        owner_token=owner_token,
    )
    device_id, first_device_token = register_ego_browser_device(client, owner_token)
    binding_id = claim_binding(
        client,
        device_id=device_id,
        device_token=first_device_token,
        tool_session_id=tool_session_id,
    )
    connect_binding(
        client,
        binding_id=binding_id,
        device_token=first_device_token,
        generation=1,
    )

    roots_digest = f"sha256:{'d' * 64}"
    policy_payload = ego_browser_device_payload(device_id)
    policy_payload.update(
        {
            "allowlist_revision": 2,
            "allowlist_roots_digest": roots_digest,
            "capabilities": [*REQUIRED_CAPABILITIES, "ego_browser_file_allowlist_v1"],
        }
    )
    policy_update = client.post(
        "/api/v1/ego-browser/devices/register",
        headers=auth_header(owner_token),
        json=policy_payload,
    )
    assert policy_update.status_code == 200, policy_update.text
    second_device_token = str(policy_update.json()["data"]["credential"]["access_token"])

    stale_credential = client.get(
        "/api/v1/ego-browser/devices",
        headers=auth_header(first_device_token),
    )
    assert stale_credential.status_code == 401
    assert stale_credential.json()["error"]["code"] == "EGO_BROWSER_CREDENTIAL_REVOKED"
    paused = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}",
        headers=auth_header(owner_token),
    )
    assert (paused.json()["data"]["status"], paused.json()["data"]["generation"]) == (
        "paused",
        2,
    )
    assert paused.json()["data"]["stop_reason"] == "local_policy_changed"

    rotated_payload = dict(policy_payload)
    rotated_payload.update(
        {
            "public_key": _ROTATED_SIGNING_PUBLIC_KEY,
            "encryption_public_key": _ROTATED_ENCRYPTION_PUBLIC_KEY,
            "generation": 2,
        }
    )
    key_rotation = client.post(
        "/api/v1/ego-browser/devices/register",
        headers=auth_header(owner_token),
        json=rotated_payload,
    )
    assert key_rotation.status_code == 200, key_rotation.text
    assert key_rotation.json()["data"]["generation"] == 2
    assert key_rotation.json()["data"]["encryption_public_key"] == (_ROTATED_ENCRYPTION_PUBLIC_KEY)
    assert key_rotation.json()["data"]["credential"]["revision"] == 3

    second_stale_credential = client.get(
        "/api/v1/ego-browser/devices",
        headers=auth_header(second_device_token),
    )
    assert second_stale_credential.status_code == 401
    assert second_stale_credential.json()["error"]["code"] == "EGO_BROWSER_CREDENTIAL_REVOKED"
    revoked = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}",
        headers=auth_header(owner_token),
    )
    assert (revoked.json()["data"]["status"], revoked.json()["data"]["generation"]) == (
        "revoked",
        3,
    )
    assert revoked.json()["data"]["stop_reason"] == "device_key_rotated"


def test_outer_admission_records_request_and_response_without_inner_content(
    client: TestClient,
) -> None:
    """外层信封 admission 必须单调、配对且不透明于内容。"""

    admin_token = bootstrap(client)
    _, owner_token = create_user(client, admin_token, "ledger-owner")
    tool_session_id, _, node_token = create_running_session(
        client,
        admin_token=admin_token,
        owner_token=owner_token,
    )
    device_id, device_token = register_ego_browser_device(client, owner_token)
    binding_id = claim_binding(
        client,
        device_id=device_id,
        device_token=device_token,
        tool_session_id=tool_session_id,
    )
    connect_binding(
        client,
        binding_id=binding_id,
        device_token=device_token,
        generation=1,
    )

    bridge_ticket_response = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/relay-ticket",
        headers=auth_header(device_token),
        json={
            "generation": 1,
            "role": "bridge",
            "ego_browser_device_id": device_id,
        },
    )
    wrapper_ticket_response = client.post(
        f"/api/v1/node-api/ego-browser/bindings/{binding_id}/relay-ticket",
        headers=auth_header(node_token),
        json={"generation": 1, "role": "wrapper"},
    )
    assert bridge_ticket_response.status_code == wrapper_ticket_response.status_code == 200

    app = cast(FastAPI, client.app)
    store = cast(InMemoryEgoBrowserRelayStore, app.state.ego_browser_relay_store)

    async def consume(raw_ticket: str) -> EgoBrowserRelayTicketClaims:
        claims = await store.consume_ticket(
            token_hash=hash_token(app.state.settings.secret_key, raw_ticket)
        )
        assert claims is not None
        return claims

    bridge_claims = asyncio.run(consume(str(bridge_ticket_response.json()["data"]["relay_ticket"])))
    wrapper_claims = asyncio.run(
        consume(str(wrapper_ticket_response.json()["data"]["relay_ticket"]))
    )
    key_wrap = base64.urlsafe_b64encode(bytes(92)).decode().rstrip("=")
    request_envelope: dict[str, object] = {
        "protocol": "ego-browser-bridge-v1",
        "channel": "ego_browser_bridge",
        "relay_binding_kind": "ego_browser",
        "binding_id": binding_id,
        "generation": 1,
        "direction": "request",
        "type": "execute",
        "request_id": "request-ledger-1",
        "sequence": 1,
        "payload_bytes": 47,
        "key_wrap": key_wrap,
    }
    response_envelope = {
        **request_envelope,
        "direction": "response",
        "type": "execute_result",
        "payload_bytes": 31,
        "key_wrap": "",
    }
    cancel_envelope = {
        **request_envelope,
        "type": "cancel",
        "payload_bytes": 29,
        "key_wrap": "",
    }

    async def admit_and_verify() -> None:
        async with app.state.session_factory() as session:
            service = EgoBrowserService(session, app.state.settings)
            await service.admit_outer_envelope(
                claims=wrapper_claims,
                envelope=request_envelope,
            )
            await service.admit_outer_envelope(
                claims=bridge_claims,
                envelope=response_envelope,
            )
            await service.admit_outer_envelope(
                claims=wrapper_claims,
                envelope=cancel_envelope,
            )
            completed_request = await session.scalar(
                select(EgoBrowserRequestLedger).where(
                    EgoBrowserRequestLedger.direction == "request",
                    EgoBrowserRequestLedger.sequence == 1,
                )
            )
            assert completed_request is not None
            assert completed_request.status == "completed"

            second_request = {
                **request_envelope,
                "request_id": "request-ledger-2",
                "sequence": 2,
            }
            second_cancel = {
                **second_request,
                "type": "cancel",
                "payload_bytes": 29,
                "key_wrap": "",
            }
            second_response = {
                **second_request,
                "direction": "response",
                "type": "execute_result",
                "payload_bytes": 31,
                "key_wrap": "",
            }
            await service.admit_outer_envelope(
                claims=wrapper_claims,
                envelope=second_request,
            )
            await service.admit_outer_envelope(
                claims=wrapper_claims,
                envelope=second_cancel,
            )
            await service.admit_outer_envelope(
                claims=bridge_claims,
                envelope=second_response,
            )
            cancelled_request = await session.scalar(
                select(EgoBrowserRequestLedger).where(
                    EgoBrowserRequestLedger.direction == "request",
                    EgoBrowserRequestLedger.sequence == 2,
                )
            )
            assert cancelled_request is not None
            assert cancelled_request.status == "cancelled"

            third_request = {
                **request_envelope,
                "request_id": "request-ledger-3",
                "sequence": 3,
            }
            await service.admit_outer_envelope(
                claims=wrapper_claims,
                envelope=third_request,
            )
            with pytest.raises(ValueError, match="sequence"):
                await service.admit_outer_envelope(
                    claims=wrapper_claims,
                    envelope=request_envelope,
                )
            orphaned_response = dict(response_envelope)
            orphaned_response["request_id"] = "missing-request"
            orphaned_response["sequence"] = 2
            with pytest.raises(ValueError, match="response_without_request"):
                await service.admit_outer_envelope(
                    claims=bridge_claims,
                    envelope=orphaned_response,
                )

            ledgers = list(
                await session.scalars(
                    select(EgoBrowserRequestLedger).order_by(EgoBrowserRequestLedger.direction)
                )
            )
            assert len(ledgers) == 5
            assert all(item.message_type != "cancel" for item in ledgers)

    asyncio.run(admit_and_verify())


def test_active_request_listing_and_cancel_enqueue_exact_node_task(client: TestClient) -> None:
    """用户取消只推进原 ledger，并幂等下发确切 Node request。"""

    admin_token = bootstrap(client)
    _, owner_token = create_user(client, admin_token, "cancel-owner")
    _, other_token = create_user(client, admin_token, "cancel-other")
    tool_session_id, _, node_token = create_running_session(
        client,
        admin_token=admin_token,
        owner_token=owner_token,
    )
    device_id, device_token = register_ego_browser_device(client, owner_token)
    binding_id = claim_binding(
        client,
        device_id=device_id,
        device_token=device_token,
        tool_session_id=tool_session_id,
    )
    connect_binding(
        client,
        binding_id=binding_id,
        device_token=device_token,
        generation=1,
    )
    app = cast(FastAPI, client.app)

    async def seed_request() -> None:
        async with app.state.session_factory() as session:
            session.add(
                EgoBrowserRequestLedger(
                    binding_id=UUID(binding_id),
                    generation=1,
                    request_id="request-cancel-api-1",
                    sequence=7,
                    direction="request",
                    message_type="execute",
                    payload_bytes=47,
                    status="accepted",
                )
            )
            await session.commit()

    asyncio.run(seed_request())

    forbidden = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}/requests",
        headers=auth_header(other_token),
    )
    assert forbidden.status_code == 404
    listed = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}/requests",
        headers=auth_header(owner_token),
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["data"]["items"]
    assert len(items) == 1
    assert {
        key: items[0][key]
        for key in (
            "binding_id",
            "generation",
            "request_id",
            "sequence",
            "message_type",
            "payload_bytes",
            "status",
        )
    } == {
        "binding_id": binding_id,
        "generation": 1,
        "request_id": "request-cancel-api-1",
        "sequence": 7,
        "message_type": "execute",
        "payload_bytes": 47,
        "status": "accepted",
    }

    path = f"/api/v1/ego-browser/bindings/{binding_id}/requests/request-cancel-api-1/cancel"
    cancelled = client.post(
        path,
        headers=auth_header(owner_token),
        json={"generation": 1, "sequence": 7},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancel_requested"
    repeated = client.post(
        path,
        headers=auth_header(owner_token),
        json={"generation": 1, "sequence": 7},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"]["status"] == "cancel_requested"

    cancel_task_ids: list[str] = []

    async def verify_persistence() -> None:
        async with app.state.session_factory() as session:
            request = await session.scalar(
                select(EgoBrowserRequestLedger).where(
                    EgoBrowserRequestLedger.request_id == "request-cancel-api-1"
                )
            )
            assert request is not None
            assert request.status == "cancel_requested"
            tasks = list(
                await session.scalars(
                    select(NodeTask).where(NodeTask.task_type == "cancel_ego_browser_request")
                )
            )
            assert len(tasks) == 1
            cancel_task_ids.append(tasks[0].task_id)
            assert tasks[0].node_id is not None
            assert tasks[0].payload == {
                "binding_id": binding_id,
                "generation": 1,
                "request_id": "request-cancel-api-1",
                "sequence": 7,
            }
            audits = list(
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "ego_browser_execute.cancel_requested"
                    )
                )
            )
            assert len(audits) == 1
            assert "script" not in json.dumps(audits[0].details)

    asyncio.run(verify_persistence())

    assert (
        client.post(
            f"/api/v1/node-api/tasks/{cancel_task_ids[0]}/start",
            headers=auth_header(node_token),
        ).status_code
        == 200
    )
    completed = client.post(
        f"/api/v1/node-api/tasks/{cancel_task_ids[0]}/complete",
        headers=auth_header(node_token),
        json={
            "result": {
                "status": "cancellation_completed",
                "request_active": False,
                "server_terminal_observed": False,
                "browser_content": "sensitive-page-secret",
            }
        },
    )
    assert completed.status_code == 200, completed.text

    async def verify_fail_closed_convergence() -> None:
        async with app.state.session_factory() as session:
            request = await session.scalar(
                select(EgoBrowserRequestLedger).where(
                    EgoBrowserRequestLedger.request_id == "request-cancel-api-1"
                )
            )
            binding = await session.get(EgoBrowserBinding, UUID(binding_id))
            outbox = await session.scalar(
                select(EgoBrowserRevocationOutbox).where(
                    EgoBrowserRevocationOutbox.binding_id == UUID(binding_id)
                )
            )
            assert request is not None and request.status == "cancelled"
            assert binding is not None
            assert (binding.status, binding.generation, binding.stop_reason) == (
                "paused",
                2,
                "request_cancel_unconfirmed",
            )
            assert outbox is not None
            assert (outbox.generation, outbox.reason) == (1, "request_cancel_unconfirmed")
            task_result = await session.scalar(
                select(NodeTaskResult).where(NodeTaskResult.task_id == cancel_task_ids[0])
            )
            assert task_result is not None
            assert task_result.result == {"status": "cancellation_unconfirmed"}
            assert task_result.error is None
            assert "sensitive-page-secret" not in json.dumps(task_result.result)

    asyncio.run(verify_fail_closed_convergence())


def test_binding_invalidation_terminalizes_only_its_nonterminal_request_rows(
    client: TestClient,
) -> None:
    """Binding generation 失效必须同步终结旧代次请求并保留已完成结果。"""

    _, _, owner_token, _, _, _, binding_id = create_active_binding(
        client,
        username="request-invalidation-owner",
    )
    app = cast(FastAPI, client.app)

    async def seed_requests() -> None:
        async with app.state.session_factory() as session:
            session.add_all(
                [
                    EgoBrowserRequestLedger(
                        binding_id=UUID(binding_id),
                        generation=1,
                        request_id=f"request-invalidation-{status}",
                        sequence=sequence,
                        direction="request",
                        message_type="execute",
                        payload_bytes=47,
                        status=status,
                    )
                    for sequence, status in enumerate(
                        ("accepted", "cancel_requested", "completed"),
                        start=1,
                    )
                ]
            )
            await session.commit()

    asyncio.run(seed_requests())
    stopped = client.post(
        f"/api/v1/ego-browser/bindings/{binding_id}/stop",
        headers=auth_header(owner_token),
        json={"generation": 1, "reason": "user_stop"},
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["data"]["status"] == "stopped"
    listed = client.get(
        f"/api/v1/ego-browser/bindings/{binding_id}/requests",
        headers=auth_header(owner_token),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["items"] == []

    async def verify_terminal_rows() -> None:
        async with app.state.session_factory() as session:
            requests = list(
                await session.scalars(
                    select(EgoBrowserRequestLedger)
                    .where(EgoBrowserRequestLedger.binding_id == UUID(binding_id))
                    .order_by(EgoBrowserRequestLedger.sequence)
                )
            )
            assert [request.status for request in requests] == [
                "cancelled",
                "cancelled",
                "completed",
            ]
            audits = list(
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "ego_browser_execute.cancelled",
                        AuditLog.target_id == binding_id,
                    )
                )
            )
            assert len(audits) == 2
            assert {audit.details["reason"] for audit in audits} == {"user_stop"}
            assert all("script" not in json.dumps(audit.details) for audit in audits)

    asyncio.run(verify_terminal_rows())
