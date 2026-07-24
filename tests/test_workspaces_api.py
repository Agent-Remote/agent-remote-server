import asyncio
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
from agent_remote_server.models import Session, SyncSession, ToolAccount, UserDevice, Workspace


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


def create_node(client: TestClient, admin_token: str) -> tuple[str, str]:
    """
    创建并注册节点

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
            "version": "0.0.4+fix.13",
        },
    )
    assert register_response.status_code == 200
    return node_id, str(register_response.json()["data"]["node_token"])


def create_workspace(client: TestClient, *, device_id: str, device_token: str) -> dict[str, object]:
    """
    创建测试 workspace

    :param client (TestClient): 测试客户端
    :param device_id (str): 设备 ID
    :param device_token (str): 设备令牌

    :return dict: workspace 数据
    """

    response = client.post(
        "/api/v1/workspaces",
        headers=auth_header(device_token),
        json={
            "device_id": device_id,
            "project_key": "sha256:test-project",
            "local_start_path": "/tmp/project",
            "display_name": "project",
        },
    )
    assert response.status_code == 200
    return dict(response.json()["data"])


def test_workspace_create_is_device_bound_and_idempotent(client: TestClient) -> None:
    admin_token = bootstrap(client)
    device_id, device_token = register_device(client, admin_token)

    workspace = create_workspace(client, device_id=device_id, device_token=device_token)
    assert workspace["device_id"] == device_id
    assert str(workspace["remote_path"]).endswith(f"/workspaces/{workspace['id']}/files")

    duplicate_response = client.post(
        "/api/v1/workspaces",
        headers=auth_header(device_token),
        json={
            "device_id": device_id,
            "project_key": "sha256:test-project",
            "local_start_path": "/tmp/project",
            "display_name": "project",
        },
    )
    assert duplicate_response.status_code == 200
    assert duplicate_response.json()["data"]["id"] == workspace["id"]

    user_token_response = client.post(
        "/api/v1/workspaces",
        headers=auth_header(admin_token),
        json={
            "device_id": device_id,
            "project_key": "sha256:other",
            "local_start_path": "/tmp/other",
            "display_name": "other",
        },
    )
    assert user_token_response.status_code == 403
    assert user_token_response.json()["error"]["code"] == "DEVICE_REQUIRED"


def test_workspace_and_failed_sync_session_can_be_deleted(client: TestClient) -> None:
    admin_token = bootstrap(client)
    node_id, _ = create_node(client, admin_token)
    device_id, device_token = register_device(client, admin_token)
    workspace = create_workspace(client, device_id=device_id, device_token=device_token)
    sync_response = client.post(
        "/api/v1/sync-sessions",
        headers=auth_header(device_token),
        json={"workspace_id": workspace["id"], "node_id": node_id},
    )
    assert sync_response.status_code == 200
    sync_id = sync_response.json()["data"]["id"]

    blocked_workspace = client.delete(
        f"/api/v1/workspaces/{workspace['id']}", headers=auth_header(device_token)
    )
    assert blocked_workspace.status_code == 409
    blocked_sync = client.delete(
        f"/api/v1/sync-sessions/{sync_id}", headers=auth_header(device_token)
    )
    assert blocked_sync.status_code == 409

    async def fail_sync() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            sync = await session.get(SyncSession, UUID(sync_id))
            assert sync is not None
            sync.status = "failed"
            await session.commit()

    asyncio.run(fail_sync())
    assert (
        client.delete(
            f"/api/v1/sync-sessions/{sync_id}", headers=auth_header(device_token)
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/v1/workspaces/{workspace['id']}", headers=auth_header(device_token)
        ).status_code
        == 200
    )


def test_sync_session_creates_prepare_workspace_task(client: TestClient) -> None:
    admin_token = bootstrap(client)
    node_id, node_token = create_node(client, admin_token)
    device_id, device_token = register_device(client, admin_token)
    workspace = create_workspace(client, device_id=device_id, device_token=device_token)

    response = client.post(
        "/api/v1/sync-sessions",
        headers=auth_header(device_token),
        json={"workspace_id": workspace["id"], "node_id": node_id},
    )
    assert response.status_code == 200
    sync = response.json()["data"]
    assert sync["workspace_id"] == workspace["id"]
    assert sync["node_id"] == node_id
    assert sync["status"] == "starting"
    assert sync["conflict_status"] == "none"
    assert sync["remote_endpoint"].startswith("agent-remote@10.42.0.10:22:/")
    assert sync["prepare_task_id"].startswith("prepare_workspace:")

    poll_response = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert poll_response.status_code == 200
    tasks = poll_response.json()["data"]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["task_type"] == "prepare_workspace"
    assert tasks[0]["payload"]["workspace_id"] == workspace["id"]
    assert tasks[0]["payload"]["sync_session_id"] == sync["id"]
    assert tasks[0]["payload"]["remote_path"] == sync["remote_path"]

    complete = client.post(
        f"/api/v1/node-api/tasks/{tasks[0]['task_id']}/complete",
        headers=auth_header(node_token),
        json={"result": {"status": "prepared", "remote_path": sync["remote_path"]}},
    )
    assert complete.status_code == 200
    ready = client.get(f"/api/v1/sync-sessions/{sync['id']}", headers=auth_header(device_token))
    assert ready.status_code == 200
    assert ready.json()["data"]["status"] == "active"

    duplicate = client.post(
        "/api/v1/sync-sessions",
        headers=auth_header(device_token),
        json={"workspace_id": workspace["id"], "node_id": node_id},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["id"] == sync["id"]


def test_sync_conflict_blocks_attach(client: TestClient) -> None:
    admin_token = bootstrap(client)
    node_id, _node_token = create_node(client, admin_token)
    device_id, device_token = register_device(client, admin_token)
    workspace = create_workspace(client, device_id=device_id, device_token=device_token)
    sync_response = client.post(
        "/api/v1/sync-sessions",
        headers=auth_header(device_token),
        json={"workspace_id": workspace["id"], "node_id": node_id},
    )
    assert sync_response.status_code == 200
    session_id = create_tool_session(client, node_id=node_id, workspace_id=str(workspace["id"]))

    async def mark_conflicted() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            sync_session = await session.scalar(select(SyncSession))
            assert sync_session is not None
            sync_session.status = "conflicted"
            sync_session.conflict_status = "has_conflicts"
            await session.commit()

    asyncio.run(mark_conflicted())

    attach_response = client.post(
        f"/api/v1/sessions/{session_id}/attach", headers=auth_header(device_token)
    )
    assert attach_response.status_code == 409
    assert attach_response.json()["error"]["code"] == "SYNC_CONFLICT"


def create_tool_session(client: TestClient, *, node_id: str, workspace_id: str) -> str:
    """
    创建测试工具 session

    :param client (TestClient): 测试客户端
    :param node_id (str): 节点 ID
    :param workspace_id (str): workspace ID

    :return str: session ID
    """

    async def create() -> str:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            workspace = await session.get(Workspace, UUID(workspace_id))
            assert workspace is not None
            device = await session.get(UserDevice, workspace.device_id)
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
            tool_session = Session(
                tool_type="claude",
                user_id=device.user_id,
                tool_account_id=account.id,
                workspace_id=workspace.id,
                node_id=UUID(node_id),
                project_key=workspace.project_key,
                status="running",
                tmux_session_name="claude-test",
                container_id="container-test",
            )
            session.add(tool_session)
            await session.flush()
            await session.commit()
            return str(tool_session.id)

    return asyncio.run(create())
