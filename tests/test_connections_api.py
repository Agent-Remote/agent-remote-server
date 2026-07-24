import asyncio
import base64
import json
from collections.abc import Iterator
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
    Session,
    SyncSession,
    ToolAccount,
    ToolAccountProfile,
    UserDevice,
    WireGuardPeer,
    Workspace,
)


async def create_schema(app: FastAPI) -> None:
    """
    创建测试数据库 schema

    :param app (FastAPI): FastAPI 应用
    """

    async with app.state.database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        secret_key="test-secret",
        log_level="CRITICAL",
        database_url="sqlite+aiosqlite:///:memory:",
        node_task_lease_seconds=30,
        node_offline_after_seconds=60,
    )
    app = create_app(settings)
    asyncio.run(create_schema(app))
    with TestClient(app) as test_client:
        yield test_client


def auth_header(token: str) -> dict[str, str]:
    """
    创建认证请求头

    :param token (str): 访问令牌

    :return dict: 请求头
    """

    return {"Authorization": f"Bearer {token}"}


def bootstrap(client: TestClient) -> str:
    """
    初始化管理员并返回令牌

    :param client (TestClient): 测试客户端

    :return str: 管理员令牌
    """

    response = client.post(
        "/api/v1/auth/bootstrap",
        json={"username": "admin", "password": "admin-secret"},
    )
    assert response.status_code == 200
    return str(response.json()["data"]["access_token"])


def create_node(client: TestClient, admin_token: str) -> tuple[str, str]:
    """
    创建并注册可连接节点

    :param client (TestClient): 测试客户端
    :param admin_token (str): 管理员令牌

    :return tuple: 节点 ID 和 node token
    """

    create_response = client.post(
        "/api/v1/nodes",
        headers=auth_header(admin_token),
        json={
            "name": "us-west-1",
            "region_code": "US",
            "tags": ["us", "west"],
            "weight": 100,
            "supported_tool_types": ["claude"],
            "wireguard_ip": "10.42.0.10",
            "wireguard_public_key": "node-wg-public",
            "wireguard_endpoint": "203.0.113.10:51820",
            "ssh_host": "10.42.0.10",
            "ssh_port": 22,
            "ssh_user": "agent-remote",
        },
    )
    assert create_response.status_code == 200
    payload = create_response.json()["data"]
    node_id = str(payload["node"]["id"])
    registration_token = str(payload["registration_token"])

    register_response = client.post(
        "/api/v1/node-api/register",
        json={
            "node_id": node_id,
            "registration_token": registration_token,
            "version": "0.0.4+fix.15",
        },
    )
    assert register_response.status_code == 200
    return node_id, str(register_response.json()["data"]["node_token"])


def register_device(client: TestClient, user_token: str) -> tuple[str, str]:
    """
    注册本地设备

    :param client (TestClient): 测试客户端
    :param user_token (str): 用户令牌

    :return tuple: 设备 ID 和设备令牌
    """

    response = client.post(
        "/api/v1/devices/register",
        headers=auth_header(user_token),
        json={
            "name": "rem-macbook",
            "platform": "macos",
            "ssh_public_key": "ssh-ed25519 AAAATESTKEY rem@test",
            "wireguard_public_key": "device-wg-public",
        },
    )
    assert response.status_code == 200
    payload = response.json()["data"]
    return str(payload["device"]["id"]), str(payload["device_token"]["access_token"])


def register_device_without_wireguard(client: TestClient, user_token: str) -> tuple[str, str]:
    """
    注册没有 WireGuard peer 的本地设备

    :param client (TestClient): 测试客户端
    :param user_token (str): 用户令牌

    :return tuple: 设备 ID 和设备令牌
    """

    response = client.post(
        "/api/v1/devices/register",
        headers=auth_header(user_token),
        json={
            "name": "repair-macbook",
            "platform": "macos",
            "ssh_public_key": "ssh-ed25519 AAAAREPAIRKEY rem@test",
        },
    )
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["wireguard_peer_id"] is None
    return str(payload["device"]["id"]), str(payload["device_token"]["access_token"])


def test_wireguard_peer_enrollment_repairs_existing_device(client: TestClient) -> None:
    admin_token = bootstrap(client)
    device_id, device_token = register_device_without_wireguard(client, admin_token)
    first_public_key = base64.b64encode(bytes(range(32))).decode("ascii")
    second_public_key = base64.b64encode(bytes(reversed(range(32)))).decode("ascii")

    missing = client.get("/api/v1/network/wireguard/config", headers=auth_header(device_token))
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "WIREGUARD_PEER_MISSING"

    forbidden = client.post(
        "/api/v1/network/wireguard/peer",
        headers=auth_header(admin_token),
        json={"public_key": first_public_key},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "DEVICE_REQUIRED"

    invalid = client.post(
        "/api/v1/network/wireguard/peer",
        headers=auth_header(device_token),
        json={"public_key": "x" * 44},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "WIREGUARD_PUBLIC_KEY_INVALID"

    enrolled = client.post(
        "/api/v1/network/wireguard/peer",
        headers=auth_header(device_token),
        json={"public_key": first_public_key},
    )
    assert enrolled.status_code == 200
    enrollment = enrolled.json()["data"]
    assert enrollment["device_id"] == device_id
    assert enrollment["interface_address"].startswith("10.77.0.")

    rotated = client.post(
        "/api/v1/network/wireguard/peer",
        headers=auth_header(device_token),
        json={"public_key": second_public_key},
    )
    assert rotated.status_code == 200
    assert rotated.json()["data"] == enrollment

    config_response = client.get(
        "/api/v1/network/wireguard/config", headers=auth_header(device_token)
    )
    assert config_response.status_code == 200

    async def inspect_state() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            peers = list(
                await session.scalars(
                    select(WireGuardPeer).where(WireGuardPeer.user_device_id == UUID(device_id))
                )
            )
            assert len(peers) == 1
            assert peers[0].public_key == second_public_key
            logs = list(
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == "network.wireguard_peer_enroll")
                )
            )
            assert len(logs) == 2
            details = json.dumps([log.details for log in logs], sort_keys=True)
            assert first_public_key not in details
            assert second_public_key not in details

    asyncio.run(inspect_state())


def test_wireguard_config_requires_device_token(client: TestClient) -> None:
    admin_token = bootstrap(client)
    create_node(client, admin_token)
    device_id, device_token = register_device(client, admin_token)

    config_response = client.get(
        "/api/v1/network/wireguard/config", headers=auth_header(device_token)
    )
    assert config_response.status_code == 200
    config = config_response.json()["data"]
    assert config["device_id"] == device_id
    assert config["interface_address"].startswith("10.77.0.")
    assert config["private_key_ref"]
    assert config["peers"][0]["public_key"] == "node-wg-public"
    assert config["peers"][0]["endpoint"] == "203.0.113.10:51820"

    user_token_response = client.get(
        "/api/v1/network/wireguard/config", headers=auth_header(admin_token)
    )
    assert user_token_response.status_code == 403
    assert user_token_response.json()["error"]["code"] == "DEVICE_REQUIRED"


def test_attach_authorization_and_node_verify(client: TestClient) -> None:
    admin_token = bootstrap(client)
    node_id, node_token = create_node(client, admin_token)
    device_id, device_token = register_device(client, admin_token)
    session_id = create_tool_session(client, node_id=node_id, device_id=device_id)

    attach_response = client.post(
        f"/api/v1/sessions/{session_id}/attach", headers=auth_header(device_token)
    )
    assert attach_response.status_code == 200
    attach = attach_response.json()["data"]
    assert attach["node_id"] == node_id
    assert attach["node_wireguard_ip"] == "10.42.0.10"
    assert attach["ssh_user"] == "agent-remote"
    assert attach["ssh_command"].startswith("ssh -tt -p 22 ")
    assert "agent-remote-attach --session" in attach["ssh_command"]
    assert attach["authorization_task_id"].startswith("sync_ssh_keys:")

    poll_response = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert poll_response.status_code == 200
    tasks = poll_response.json()["data"]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["task_type"] == "sync_ssh_keys"
    assert tasks[0]["payload"]["device_id"] == device_id
    assert tasks[0]["payload"]["ssh_keys"][0]["forced_command"] == (
        f"agent-remote-attach --device {device_id}"
    )

    verify_response = client.post(
        "/api/v1/node-api/attach/verify",
        headers=auth_header(node_token),
        json={"node_id": node_id, "session_id": session_id, "device_id": device_id},
    )
    assert verify_response.status_code == 200
    assert verify_response.json()["data"]["tmux_session_name"] == "claude-test"

    revoke_response = client.post(
        f"/api/v1/devices/{device_id}/disable", headers=auth_header(admin_token)
    )
    assert revoke_response.status_code == 200

    denied_verify = client.post(
        "/api/v1/node-api/attach/verify",
        headers=auth_header(node_token),
        json={"node_id": node_id, "session_id": session_id, "device_id": device_id},
    )
    assert denied_verify.status_code == 403
    assert denied_verify.json()["error"]["code"] == "DEVICE_REVOKED"


def test_sync_gateway_requires_device_node_and_active_sync(client: TestClient) -> None:
    admin_token = bootstrap(client)
    node_id, node_token = create_node(client, admin_token)
    device_id, _device_token = register_device(client, admin_token)
    create_tool_session(client, node_id=node_id, device_id=device_id)

    denied = client.post(
        "/api/v1/node-api/sync/verify",
        headers=auth_header(node_token),
        json={"node_id": node_id, "device_id": device_id},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "SYNC_ACCESS_DENIED"

    async def create_sync_session() -> str:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            workspace = await session.scalar(
                select(Workspace).where(Workspace.device_id == UUID(device_id))
            )
            assert workspace is not None
            sync = SyncSession(
                user_id=workspace.user_id,
                workspace_id=workspace.id,
                node_id=UUID(node_id),
                local_path=workspace.local_start_path,
                status="active",
                conflict_status="none",
                remote_path=workspace.remote_path,
                sync_mode="two-way-resolved",
            )
            session.add(sync)
            await session.flush()
            sync_id = str(sync.id)
            await session.commit()
            return sync_id

    sync_id = asyncio.run(create_sync_session())
    verified = client.post(
        "/api/v1/node-api/sync/verify",
        headers=auth_header(node_token),
        json={"node_id": node_id, "device_id": device_id},
    )
    assert verified.status_code == 200
    assert verified.json()["data"]["user_id"]

    async def stop_sync_session() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            sync = await session.get(SyncSession, UUID(sync_id))
            assert sync is not None
            sync.status = "stopped"
            await session.commit()

    asyncio.run(stop_sync_session())
    stopped = client.post(
        "/api/v1/node-api/sync/verify",
        headers=auth_header(node_token),
        json={"node_id": node_id, "device_id": device_id},
    )
    assert stopped.status_code == 403
    assert stopped.json()["error"]["code"] == "SYNC_ACCESS_DENIED"


def test_native_binding_attach_verifies_device_and_returns_private_session(
    client: TestClient,
) -> None:
    admin_token = bootstrap(client)
    node_id, node_token = create_node(client, admin_token)
    device_id, _device_token = register_device(client, admin_token)

    async def create_binding() -> str:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            device = await session.get(UserDevice, UUID(device_id))
            assert device is not None
            account = ToolAccount(
                user_id=device.user_id,
                tool_type="claude",
                display_name="Native Binding",
                status="binding_waiting_user_login",
                region_code="US",
                timezone="America/Los_Angeles",
                locale="en-US",
                preferred_node_tags=["us"],
                affinity_node_id=UUID(node_id),
                runtime_backend="native",
            )
            session.add(account)
            await session.flush()
            session.add(
                ToolAccountProfile(
                    tool_account_id=account.id,
                    tool_type="claude",
                    profile_json={
                        "binding_session_id": f"bind-{account.id}",
                        "tmux_session_name": "bind-native",
                    },
                    encrypted_secrets=None,
                )
            )
            account_id = str(account.id)
            await session.commit()
            return account_id

    account_id = asyncio.run(create_binding())
    response = client.post(
        "/api/v1/node-api/binding-attach/verify",
        headers=auth_header(node_token),
        json={
            "node_id": node_id,
            "tool_account_id": account_id,
            "device_id": device_id,
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["binding_session_id"] == f"bind-{account_id}"
    assert data["tmux_session_name"] == "bind-native"
    assert data["runtime_backend"] == "native"


def create_tool_session(client: TestClient, *, node_id: str, device_id: str) -> str:
    """
    创建测试工具 session

    :param client (TestClient): 测试客户端
    :param node_id (str): 节点 ID
    :param device_id (str): 设备 ID

    :return str: session ID
    """

    async def create() -> str:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            device = await session.get(UserDevice, UUID(device_id))
            assert device is not None
            account = ToolAccount(
                user_id=device.user_id,
                tool_type="claude",
                display_name="Claude Test",
                status="active",
                region_code="US",
                timezone="America/Los_Angeles",
                locale="en-US",
                preferred_node_tags=["us"],
                affinity_node_id=UUID(node_id),
            )
            session.add(account)
            await session.flush()
            workspace = Workspace(
                user_id=device.user_id,
                device_id=device.id,
                project_key="project-test",
                local_start_path="/tmp/project-test",
                display_name="Project Test",
                remote_path="/srv/agent-remote/workspaces/project-test",
            )
            session.add(workspace)
            await session.flush()
            tool_session = Session(
                tool_type="claude",
                user_id=device.user_id,
                tool_account_id=account.id,
                workspace_id=workspace.id,
                node_id=UUID(node_id),
                project_key="project-test",
                status="running",
                tmux_session_name="claude-test",
                container_id="container-test",
            )
            session.add(tool_session)
            await session.flush()
            created_id = str(tool_session.id)
            await session.commit()
            return created_id

    return asyncio.run(create())
