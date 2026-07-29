import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.main import create_app
from agent_remote_server.models import (
    AuditLog,
    Node,
    PortForward,
    Session,
    ToolAccount,
    User,
    Workspace,
)
from agent_remote_server.port_forward_tokens import (
    InMemoryPortForwardTokenStore,
    PortForwardTokenClaims,
)
from agent_remote_server.services.port_forwards import PortForwardService

NODE_TOKEN = "node-port-forward-test-token"


def test_in_memory_token_store_enforces_one_time_ttl_rate_and_cleanup() -> None:
    """内存实现必须与生产 Redis store 保持一次性和限速语义一致。"""

    async def exercise() -> None:
        store = InMemoryPortForwardTokenStore()
        claims = PortForwardTokenClaims(
            forward_id=UUID("11111111-1111-4111-8111-111111111111"),
            device_id=UUID("22222222-2222-4222-8222-222222222222"),
            ssh_key_id=None,
        )
        await store.issue(token_hash="once", claims=claims, ttl=60)
        with pytest.raises(RuntimeError, match="collision"):
            await store.issue(token_hash="once", claims=claims, ttl=60)
        assert await store.consume(token_hash="once") == claims
        assert await store.consume(token_hash="once") is None

        store._values["expired"] = (claims, datetime.now(UTC) - timedelta(seconds=1))
        assert await store.consume(token_hash="expired") is None
        assert await store.allow(scope="device", limit=1, window_seconds=60)
        assert not await store.allow(scope="device", limit=1, window_seconds=60)
        store._rates["reset"] = (99, datetime.now(UTC) - timedelta(seconds=1))
        assert await store.allow(scope="reset", limit=1, window_seconds=60)

        await store.issue(token_hash="cleanup", claims=claims, ttl=60)
        await store.close()
        assert store._values == {}
        assert store._rates == {}

    asyncio.run(exercise())


async def create_schema(app: FastAPI) -> None:
    """创建测试数据库 schema。"""

    async with app.state.database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """创建使用内存 token store 的测试客户端。"""

    settings = Settings(
        secret_key="test-secret",
        log_level="CRITICAL",
        database_url="sqlite+aiosqlite:///:memory:",
        port_forward_max_per_session=1,
    )
    app = create_app(settings)
    app.state.port_forward_token_store = InMemoryPortForwardTokenStore()
    asyncio.run(create_schema(app))
    with TestClient(app) as test_client:
        yield test_client


def auth_header(token: str) -> dict[str, str]:
    """创建 Bearer 请求头。"""

    return {"Authorization": f"Bearer {token}"}


def bootstrap(client: TestClient) -> str:
    """初始化管理员并返回用户 token。"""

    response = client.post(
        "/api/v1/auth/bootstrap",
        json={"username": "admin", "password": "admin-secret"},
    )
    assert response.status_code == 200
    return str(response.json()["data"]["access_token"])


def register_device(client: TestClient, user_token: str) -> tuple[str, str, str]:
    """注册设备并返回设备、token 和 SSH key ID。"""

    response = client.post(
        "/api/v1/devices/register",
        headers=auth_header(user_token),
        json={
            "name": "forward-macbook",
            "platform": "macos",
            "ssh_public_key": "ssh-ed25519 AAAAPORTFORWARD rem@test",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    return (
        str(data["device"]["id"]),
        str(data["device_token"]["access_token"]),
        str(data["ssh_key_id"]),
    )


async def seed_running_session(
    client: TestClient, *, device_id: str, capability: bool = True
) -> tuple[str, str]:
    """写入具备端口转发条件的 Node 和运行 session。"""

    app = cast(FastAPI, client.app)
    async with app.state.session_factory() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        assert user is not None
        node = Node(
            name="forward-node",
            status="healthy",
            region_code="US",
            tags=[],
            weight=100,
            wireguard_ip="10.77.0.20",
            ssh_port=22,
            ssh_user="agent-remote",
            supported_tool_types=["claude"],
            allowed_runtime_backends=["native"],
            default_runtime_backend="native",
            runtime_policy={},
            runtime_capabilities=(
                {
                    "session_port_forwarding": {
                        "supported": True,
                        "protocol_versions": [1],
                        "backends": ["native"],
                        "max_streams": 128,
                    }
                }
                if capability
                else {}
            ),
            node_token_hash=None,
        )
        session.add(node)
        await session.flush()
        account = ToolAccount(
            user_id=user.id,
            tool_type="claude",
            display_name="Claude",
            status="active",
            region_code="US",
            timezone="UTC",
            locale="en-US",
            preferred_node_tags=[],
            affinity_node_id=node.id,
            runtime_backend="native",
        )
        workspace = Workspace(
            user_id=user.id,
            device_id=UUID(device_id),
            project_key="port-forward-project",
            local_start_path="/tmp/port-forward-project",
            display_name="port-forward-project",
            remote_path="/srv/agent-remote/workspaces/port-forward-project",
            sync_git=True,
            git_sync_policy={},
        )
        session.add_all([account, workspace])
        await session.flush()
        tool_session = Session(
            tool_type="claude",
            user_id=user.id,
            tool_account_id=account.id,
            workspace_id=workspace.id,
            node_id=node.id,
            project_key=workspace.project_key,
            status="running",
            tmux_session_name="ar-session-forward",
            runtime_backend="native",
            runtime_resource_id="native-runtime-forward",
        )
        session.add(tool_session)
        await session.commit()
        return str(tool_session.id), str(node.id)


async def set_node_token(client: TestClient, node_id: str) -> None:
    """为测试 Node 设置可认证 token。"""

    from agent_remote_server.security import hash_token

    app = cast(FastAPI, client.app)
    async with app.state.session_factory() as session:
        node = await session.get(Node, UUID(node_id))
        assert node is not None
        node.node_token_hash = hash_token(app.state.settings.secret_key, NODE_TOKEN)
        await session.commit()


def create_forward(client: TestClient, device_token: str, session_id: str) -> dict[str, object]:
    """创建测试端口转发。"""

    response = client.post(
        f"/api/v1/sessions/{session_id}/port-forwards",
        headers=auth_header(device_token),
        json={
            "remote_port": 5173,
            "local_port": 5173,
            "client_instance_id": "test-cli-instance",
            "ttl_seconds": 3600,
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return cast(dict[str, object], response.json()["data"])


def test_port_forward_full_lifecycle_and_generation_fencing(client: TestClient) -> None:
    user_token = bootstrap(client)
    device_id, device_token, ssh_key_id = register_device(client, user_token)
    session_id, node_id = asyncio.run(seed_running_session(client, device_id=device_id))
    asyncio.run(set_node_token(client, node_id))

    created = create_forward(client, device_token, session_id)
    forward_id = str(created["id"])
    connect_token = str(cast(dict[str, object], created["connection"])["token"])
    assert created["node_wireguard_ip"] == "10.77.0.20"
    assert created["ssh_user"] == "agent-remote"

    wrong_identity = client.post(
        "/api/v1/node-api/port-forwards/redeem",
        headers=auth_header(NODE_TOKEN),
        json={
            "forward_id": forward_id,
            "device_id": device_id,
            "ssh_key_id": "00000000-0000-0000-0000-000000000001",
            "connect_token": connect_token,
        },
    )
    assert wrong_identity.status_code == 403

    redeemed = client.post(
        "/api/v1/node-api/port-forwards/redeem",
        headers=auth_header(NODE_TOKEN),
        json={
            "forward_id": forward_id,
            "device_id": device_id,
            "ssh_key_id": ssh_key_id,
            "connect_token": connect_token,
        },
    )
    assert redeemed.status_code == 200, redeemed.text
    lease = redeemed.json()["data"]
    assert lease["generation"] == 1
    assert lease["remote_port"] == 5173
    assert lease["runtime_resource_id"] == "native-runtime-forward"
    assert lease["control_plane_grace_seconds"] == 300

    replay = client.post(
        "/api/v1/node-api/port-forwards/redeem",
        headers=auth_header(NODE_TOKEN),
        json={
            "forward_id": forward_id,
            "device_id": device_id,
            "ssh_key_id": ssh_key_id,
            "connect_token": connect_token,
        },
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "AUTH_INVALID"

    renewed = client.post(
        f"/api/v1/node-api/port-forwards/{forward_id}/renew",
        headers=auth_header(NODE_TOKEN),
        json={
            "generation": 1,
            "bytes_up_total": 100,
            "bytes_down_total": 200,
            "connection_count_total": 2,
        },
    )
    assert renewed.status_code == 200

    duplicate_renew = client.post(
        f"/api/v1/node-api/port-forwards/{forward_id}/renew",
        headers=auth_header(NODE_TOKEN),
        json={
            "generation": 1,
            "bytes_up_total": 100,
            "bytes_down_total": 200,
            "connection_count_total": 2,
        },
    )
    assert duplicate_renew.status_code == 200

    reconnect = client.post(
        f"/api/v1/port-forwards/{forward_id}/connections",
        headers=auth_header(device_token),
    )
    assert reconnect.status_code == 200
    assert reconnect.headers["cache-control"] == "no-store"
    reconnect_token = reconnect.json()["data"]["token"]
    second = client.post(
        "/api/v1/node-api/port-forwards/redeem",
        headers=auth_header(NODE_TOKEN),
        json={
            "forward_id": forward_id,
            "device_id": device_id,
            "ssh_key_id": ssh_key_id,
            "connect_token": reconnect_token,
        },
    )
    assert second.status_code == 200
    assert second.json()["data"]["generation"] == 2

    stale = client.post(
        f"/api/v1/node-api/port-forwards/{forward_id}/renew",
        headers=auth_header(NODE_TOKEN),
        json={"generation": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "TUNNEL_EXPIRED"

    released = client.post(
        f"/api/v1/node-api/port-forwards/{forward_id}/release",
        headers=auth_header(NODE_TOKEN),
        json={
            "generation": 2,
            "bytes_up_total": 10,
            "bytes_down_total": 20,
            "connection_count_total": 1,
            "reason": "ssh_disconnected",
        },
    )
    assert released.status_code == 200

    stopped = client.delete(
        f"/api/v1/port-forwards/{forward_id}", headers=auth_header(device_token)
    )
    assert stopped.status_code == 200
    assert stopped.json()["data"]["status"] == "stopped"
    assert stopped.json()["data"]["bytes_up"] == 110
    assert stopped.json()["data"]["bytes_down"] == 220
    assert stopped.json()["data"]["connection_count"] == 3

    async def inspect() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            value = await session.get(PortForward, UUID(forward_id))
            assert value is not None
            assert str(value.ssh_key_id) == ssh_key_id
            assert not hasattr(value, "connect_token")
            logs = list(await session.scalars(select(AuditLog)))
            serialized = json.dumps([log.details for log in logs], sort_keys=True)
            assert connect_token not in serialized
            assert reconnect_token not in serialized

    asyncio.run(inspect())


def test_port_forward_cleanup_expires_disconnects_and_revokes(client: TestClient) -> None:
    user_token = bootstrap(client)
    device_id, device_token, ssh_key_id = register_device(client, user_token)
    session_id, node_id = asyncio.run(seed_running_session(client, device_id=device_id))
    asyncio.run(set_node_token(client, node_id))

    expired = create_forward(client, device_token, session_id)
    expired_id = str(expired["id"])

    async def expire_and_cleanup() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            value = await session.get(PortForward, UUID(expired_id))
            assert value is not None
            value.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        async with app.state.session_factory() as session:
            result = await PortForwardService(
                session, app.state.settings, app.state.port_forward_token_store
            ).cleanup()
            assert result.changed == 1
        async with app.state.session_factory() as session:
            value = await session.get(PortForward, UUID(expired_id))
            assert value is not None
            assert value.status == "expired"

    asyncio.run(expire_and_cleanup())

    active = create_forward(client, device_token, session_id)
    active_id = str(active["id"])
    connect_token = str(cast(dict[str, object], active["connection"])["token"])
    redeemed = client.post(
        "/api/v1/node-api/port-forwards/redeem",
        headers=auth_header(NODE_TOKEN),
        json={
            "forward_id": active_id,
            "device_id": device_id,
            "ssh_key_id": ssh_key_id,
            "connect_token": connect_token,
        },
    )
    assert redeemed.status_code == 200

    async def disconnect_and_revoke() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            value = await session.get(PortForward, UUID(active_id))
            assert value is not None
            value.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        async with app.state.session_factory() as session:
            result = await PortForwardService(
                session, app.state.settings, app.state.port_forward_token_store
            ).cleanup()
            assert result.changed == 1
        async with app.state.session_factory() as session:
            value = await session.get(PortForward, UUID(active_id))
            user = await session.scalar(select(User).where(User.username == "admin"))
            assert value is not None and user is not None
            assert value.status == "disconnected"
            user.status = "disabled"
            await session.commit()
        async with app.state.session_factory() as session:
            result = await PortForwardService(
                session, app.state.settings, app.state.port_forward_token_store
            ).cleanup()
            assert result.changed == 1
        async with app.state.session_factory() as session:
            value = await session.get(PortForward, UUID(active_id))
            assert value is not None
            assert value.status == "revoked"
            assert value.stop_reason == "user_revoked"

    asyncio.run(disconnect_and_revoke())


def test_port_forward_cleanup_rotates_past_healthy_batches(client: TestClient) -> None:
    """健康的首批记录不能让后续待回收记录长期饥饿。"""

    user_token = bootstrap(client)
    device_id, device_token, _ssh_key_id = register_device(client, user_token)
    first_session_id, _ = asyncio.run(seed_running_session(client, device_id=device_id))
    second_session_id, _ = asyncio.run(seed_running_session(client, device_id=device_id))
    first = create_forward(client, device_token, first_session_id)
    second = create_forward(client, device_token, second_session_id)
    earlier_id, later_id = sorted((UUID(str(first["id"])), UUID(str(second["id"]))))

    async def rotate_cleanup() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            later = await session.get(PortForward, later_id)
            assert later is not None
            later.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        async with app.state.session_factory() as session:
            first_result = await PortForwardService(
                session, app.state.settings, app.state.port_forward_token_store
            ).cleanup(limit=1)
            assert first_result.changed == 0
            assert first_result.next_cursor == earlier_id
        async with app.state.session_factory() as session:
            second_result = await PortForwardService(
                session, app.state.settings, app.state.port_forward_token_store
            ).cleanup(limit=1, after_id=earlier_id)
            assert second_result.changed == 1
        async with app.state.session_factory() as session:
            later = await session.get(PortForward, later_id)
            assert later is not None
            assert later.status == "expired"

    asyncio.run(rotate_cleanup())


def test_port_forward_enforces_device_capability_port_and_quota(client: TestClient) -> None:
    user_token = bootstrap(client)
    device_id, device_token, _ssh_key_id = register_device(client, user_token)
    unsupported_session, _node_id = asyncio.run(
        seed_running_session(client, device_id=device_id, capability=False)
    )

    user_token_request = client.post(
        f"/api/v1/sessions/{unsupported_session}/port-forwards",
        headers=auth_header(user_token),
        json={
            "remote_port": 5173,
            "local_port": 5173,
            "client_instance_id": "user-token",
        },
    )
    assert user_token_request.status_code == 403
    assert user_token_request.json()["error"]["code"] == "DEVICE_REQUIRED"

    unsupported = client.post(
        f"/api/v1/sessions/{unsupported_session}/port-forwards",
        headers=auth_header(device_token),
        json={
            "remote_port": 5173,
            "local_port": 5173,
            "client_instance_id": "unsupported",
        },
    )
    assert unsupported.status_code == 409
    assert unsupported.json()["error"]["code"] == "PORT_FORWARD_UNSUPPORTED"

    session_id, _node_id = asyncio.run(seed_running_session(client, device_id=device_id))
    denied = client.post(
        f"/api/v1/sessions/{session_id}/port-forwards",
        headers=auth_header(device_token),
        json={"remote_port": 80, "local_port": 8080, "client_instance_id": "denied"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PORT_NOT_ALLOWED"

    create_forward(client, device_token, session_id)
    quota = client.post(
        f"/api/v1/sessions/{session_id}/port-forwards",
        headers=auth_header(device_token),
        json={"remote_port": 5174, "local_port": 5174, "client_instance_id": "quota"},
    )
    assert quota.status_code == 409
    assert quota.json()["error"]["code"] == "POLICY_LIMIT"


def test_port_forward_requests_reject_unknown_target_fields(client: TestClient) -> None:
    """客户端不能通过未声明字段扩展固定 loopback 目标。"""

    user_token = bootstrap(client)
    device_id, device_token, _ssh_key_id = register_device(client, user_token)
    session_id, _node_id = asyncio.run(seed_running_session(client, device_id=device_id))
    response = client.post(
        f"/api/v1/sessions/{session_id}/port-forwards",
        headers=auth_header(device_token),
        json={
            "remote_port": 5173,
            "local_port": 5173,
            "client_instance_id": "unknown-target",
            "host": "169.254.169.254",
        },
    )
    assert response.status_code == 422


def test_port_forward_create_rate_limit_returns_stable_error(client: TestClient) -> None:
    """创建限速必须在生成额外授权前返回稳定错误。"""

    app = cast(FastAPI, client.app)
    app.state.settings.port_forward_create_rate_limit_per_minute = 1
    user_token = bootstrap(client)
    device_id, device_token, _ssh_key_id = register_device(client, user_token)
    first_session_id, _ = asyncio.run(seed_running_session(client, device_id=device_id))
    second_session_id, _ = asyncio.run(seed_running_session(client, device_id=device_id))
    create_forward(client, device_token, first_session_id)
    limited = client.post(
        f"/api/v1/sessions/{second_session_id}/port-forwards",
        headers=auth_header(device_token),
        json={
            "remote_port": 5174,
            "local_port": 5174,
            "client_instance_id": "rate-limited",
        },
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


def test_revoked_device_invalidates_active_forward_lease(client: TestClient) -> None:
    user_token = bootstrap(client)
    device_id, device_token, ssh_key_id = register_device(client, user_token)
    session_id, node_id = asyncio.run(seed_running_session(client, device_id=device_id))
    asyncio.run(set_node_token(client, node_id))
    created = create_forward(client, device_token, session_id)
    forward_id = str(created["id"])
    connect_token = str(cast(dict[str, object], created["connection"])["token"])
    redeemed = client.post(
        "/api/v1/node-api/port-forwards/redeem",
        headers=auth_header(NODE_TOKEN),
        json={
            "forward_id": forward_id,
            "device_id": device_id,
            "ssh_key_id": ssh_key_id,
            "connect_token": connect_token,
        },
    )
    assert redeemed.status_code == 200

    revoked = client.post(f"/api/v1/devices/{device_id}/disable", headers=auth_header(user_token))
    assert revoked.status_code == 200
    listed = client.get("/api/v1/port-forwards", headers=auth_header(user_token))
    assert listed.status_code == 200
    assert listed.json()["data"]["items"][0]["status"] == "revoked"
    assert listed.json()["data"]["items"][0]["stop_reason"] == "device_revoked"
    renew = client.post(
        f"/api/v1/node-api/port-forwards/{forward_id}/renew",
        headers=auth_header(NODE_TOKEN),
        json={"generation": 1},
    )
    assert renew.status_code == 409
    assert renew.json()["error"]["code"] == "TUNNEL_EXPIRED"


def test_stopping_session_immediately_revokes_forward(client: TestClient) -> None:
    """显式停止 session 的事务必须同步收敛关联转发。"""

    user_token = bootstrap(client)
    device_id, device_token, _ssh_key_id = register_device(client, user_token)
    session_id, _node_id = asyncio.run(seed_running_session(client, device_id=device_id))
    created = create_forward(client, device_token, session_id)
    stopped = client.post(
        f"/api/v1/sessions/{session_id}/stop",
        headers=auth_header(device_token),
    )
    assert stopped.status_code == 200, stopped.text
    forward = client.get(
        f"/api/v1/port-forwards/{created['id']}",
        headers=auth_header(device_token),
    )
    assert forward.status_code == 200
    assert forward.json()["data"]["status"] == "revoked"
    assert forward.json()["data"]["stop_reason"] == "session_not_running"


def test_disabling_node_immediately_revokes_forward(client: TestClient) -> None:
    """管理员禁用 Node 时关联转发必须在同一事务进入终态。"""

    user_token = bootstrap(client)
    device_id, device_token, _ssh_key_id = register_device(client, user_token)
    session_id, node_id = asyncio.run(seed_running_session(client, device_id=device_id))
    created = create_forward(client, device_token, session_id)
    disabled = client.patch(
        f"/api/v1/nodes/{node_id}",
        headers=auth_header(user_token),
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200, disabled.text
    forward = client.get(
        f"/api/v1/port-forwards/{created['id']}",
        headers=auth_header(device_token),
    )
    assert forward.status_code == 200
    assert forward.json()["data"]["status"] == "revoked"
    assert forward.json()["data"]["stop_reason"] == "node_revoked"


def test_expired_forward_is_persisted_before_connection_is_rejected(client: TestClient) -> None:
    """过期检查必须持久化终态和审计后再拒绝重连。"""

    user_token = bootstrap(client)
    device_id, device_token, _ssh_key_id = register_device(client, user_token)
    session_id, _node_id = asyncio.run(seed_running_session(client, device_id=device_id))
    created = create_forward(client, device_token, session_id)
    forward_id = str(created["id"])

    async def expire() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            value = await session.get(PortForward, UUID(forward_id))
            assert value is not None
            value.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire())
    response = client.post(
        f"/api/v1/port-forwards/{forward_id}/connections",
        headers=auth_header(device_token),
    )
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "AUTH_EXPIRED"

    async def inspect() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            value = await session.get(PortForward, UUID(forward_id))
            assert value is not None
            assert value.status == "expired"
            assert value.stop_reason == "ttl_expired"
            audit = await session.scalar(
                select(AuditLog).where(AuditLog.action == "port_forward.expired")
            )
            assert audit is not None

    asyncio.run(inspect())


def test_capability_requires_protocol_and_caps_policy_streams(client: TestClient) -> None:
    """不兼容协议必须拒绝，策略并发不得超过 Node capability。"""

    user_token = bootstrap(client)
    device_id, device_token, ssh_key_id = register_device(client, user_token)
    session_id, node_id = asyncio.run(seed_running_session(client, device_id=device_id))
    asyncio.run(set_node_token(client, node_id))

    async def configure(*, versions: list[int], max_streams: int) -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            node = await session.get(Node, UUID(node_id))
            assert node is not None
            node.runtime_capabilities = {
                "session_port_forwarding": {
                    "supported": True,
                    "protocol_versions": versions,
                    "backends": ["native"],
                    "max_streams": max_streams,
                }
            }
            node.runtime_policy = {"port_forwarding": {"max_streams": 500}}
            await session.commit()

    asyncio.run(configure(versions=[2], max_streams=16))
    incompatible = client.post(
        f"/api/v1/sessions/{session_id}/port-forwards",
        headers=auth_header(device_token),
        json={"remote_port": 5173, "local_port": 5173, "client_instance_id": "protocol"},
    )
    assert incompatible.status_code == 409
    assert incompatible.json()["error"]["code"] == "PROTOCOL_UNSUPPORTED"

    asyncio.run(configure(versions=[1], max_streams=16))
    created = create_forward(client, device_token, session_id)
    redeemed = client.post(
        "/api/v1/node-api/port-forwards/redeem",
        headers=auth_header(NODE_TOKEN),
        json={
            "forward_id": str(created["id"]),
            "device_id": device_id,
            "ssh_key_id": ssh_key_id,
            "connect_token": str(cast(dict[str, object], created["connection"])["token"]),
        },
    )
    assert redeemed.status_code == 200
    assert redeemed.json()["data"]["max_streams"] == 16
