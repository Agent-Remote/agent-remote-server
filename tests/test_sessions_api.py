import asyncio
from collections.abc import Iterator
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.main import create_app
from agent_remote_server.models import Node, Session, ToolAccount


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


def create_node(client: TestClient, token: str, *, name: str, weight: int) -> tuple[str, str]:
    """
    创建并注册测试节点

    :param client (TestClient): 测试客户端
    :param token (str): 管理员令牌
    :param name (str): 节点名称
    :param weight (int): 调度权重
    :return tuple: 节点 ID 和 node token
    """

    response = client.post(
        "/api/v1/nodes",
        headers=auth_header(token),
        json={
            "name": name,
            "region_code": "US",
            "tags": ["us"],
            "weight": weight,
            "supported_tool_types": ["claude"],
            "wireguard_ip": f"10.42.0.{weight}",
            "ssh_host": f"10.42.0.{weight}",
            "ssh_port": 22,
            "ssh_user": "agent-remote",
        },
    )
    assert response.status_code == 200
    node = response.json()["data"]
    node_id = str(node["node"]["id"])
    register = client.post(
        "/api/v1/node-api/register",
        json={
            "node_id": node_id,
            "registration_token": node["registration_token"],
            "version": "0.0.4+fix.14",
        },
    )
    assert register.status_code == 200
    return node_id, str(register.json()["data"]["node_token"])


def register_device(client: TestClient, token: str) -> tuple[str, str]:
    """
    注册测试设备

    :param client (TestClient): 测试客户端
    :param token (str): 用户令牌
    :return tuple: 设备 ID 和设备令牌
    """

    response = client.post(
        "/api/v1/devices/register",
        headers=auth_header(token),
        json={
            "name": "local-mac",
            "platform": "macos",
            "ssh_public_key": "ssh-ed25519 AAAATESTKEY local@test",
            "wireguard_public_key": "device-wg-public",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    return str(data["device"]["id"]), str(data["device_token"]["access_token"])


def create_workspace(
    client: TestClient, device_token: str, device_id: str, project_key: str
) -> str:
    """
    创建测试 workspace

    :param client (TestClient): 测试客户端
    :param device_token (str): 设备令牌
    :param device_id (str): 设备 ID
    :param project_key (str): 项目 key
    :return str: workspace ID
    """

    response = client.post(
        "/api/v1/workspaces",
        headers=auth_header(device_token),
        json={
            "device_id": device_id,
            "project_key": project_key,
            "local_start_path": "/tmp/project",
            "display_name": "Project",
        },
    )
    assert response.status_code == 200
    return str(response.json()["data"]["id"])


def create_account(client: TestClient, token: str) -> str:
    """
    创建并激活测试工具账户

    :param client (TestClient): 测试客户端
    :param token (str): 用户令牌
    :return str: 工具账户 ID
    """

    response = client.post(
        "/api/v1/tool-accounts",
        headers=auth_header(token),
        json={
            "tool_type": "claude",
            "display_name": "Claude US",
            "region_code": "US",
            "timezone": "America/Los_Angeles",
            "locale": "en_US.UTF-8",
            "preferred_node_tags": ["us"],
        },
    )
    assert response.status_code == 200
    account_id = str(response.json()["data"]["id"])

    async def activate() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            account = await session.get(ToolAccount, UUID(account_id))
            assert account is not None
            account.status = "active"
            await session.commit()

    asyncio.run(activate())
    return account_id


def test_create_session_polls_create_tool_session_task(client: TestClient) -> None:
    token = bootstrap(client)
    node_id, node_token = create_node(client, token, name="us-west-1", weight=10)
    device_id, device_token = register_device(client, token)
    workspace_id = create_workspace(client, device_token, device_id, "sha256:project")
    account_id = create_account(client, token)

    response = client.post(
        "/api/v1/sessions",
        headers=auth_header(token),
        json={
            "tool_type": "claude",
            "tool_account_id": account_id,
            "workspace_id": workspace_id,
            "project_key": "sha256:project",
            "argv": ["--model", "opus"],
        },
    )
    assert response.status_code == 200
    tool_session = response.json()["data"]
    assert tool_session["status"] == "starting"
    assert tool_session["node_id"] == node_id
    assert tool_session["tmux_session_name"].startswith("ar-claude-")

    poll_response = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert poll_response.status_code == 200
    tasks = poll_response.json()["data"]["tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["task_type"] == "create_tool_session"
    assert task["payload"]["session_id"] == tool_session["id"]
    assert task["payload"]["tool_account_id"] == account_id
    assert task["payload"]["argv"] == ["--model", "opus"]
    assert task["payload"]["template"]["command"] == ["claude", "--model", "opus"]
    assert task["payload"]["timezone"] == "America/Los_Angeles"

    complete_response = client.post(
        f"/api/v1/node-api/tasks/{task['task_id']}/complete",
        headers=auth_header(node_token),
        json={
            "result": {
                "status": "running",
                "session_id": tool_session["id"],
                "tmux_session_name": task["payload"]["tmux_session_name"],
                "sandbox_name": task["payload"]["sandbox_name"],
            }
        },
    )
    assert complete_response.status_code == 200

    current_response = client.get(
        "/api/v1/sessions/current-project",
        headers=auth_header(token),
        params={"tool_type": "claude", "project_key": "sha256:project"},
    )
    assert current_response.status_code == 200
    current = current_response.json()["data"]
    assert current["id"] == tool_session["id"]
    assert current["status"] == "running"


def test_create_session_failure_does_not_fail_tool_account(client: TestClient) -> None:
    token = bootstrap(client)
    _node_id, node_token = create_node(client, token, name="us-west-1", weight=10)
    device_id, device_token = register_device(client, token)
    workspace_id = create_workspace(client, device_token, device_id, "sha256:failed-session")
    account_id = create_account(client, token)

    response = client.post(
        "/api/v1/sessions",
        headers=auth_header(token),
        json={
            "tool_type": "claude",
            "tool_account_id": account_id,
            "workspace_id": workspace_id,
            "project_key": "sha256:failed-session",
            "argv": [],
        },
    )
    assert response.status_code == 200
    tool_session = response.json()["data"]

    poll_response = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    task = poll_response.json()["data"]["tasks"][0]
    fail_response = client.post(
        f"/api/v1/node-api/tasks/{task['task_id']}/fail",
        headers=auth_header(node_token),
        json={"error": {"code": "RUNTIME_FAILED", "message": "session startup failed"}},
    )
    assert fail_response.status_code == 200

    session_response = client.get(
        f"/api/v1/sessions/{tool_session['id']}",
        headers=auth_header(token),
    )
    assert session_response.status_code == 200
    assert session_response.json()["data"]["status"] == "failed"

    account_response = client.get(
        f"/api/v1/tool-accounts/{account_id}",
        headers=auth_header(token),
    )
    assert account_response.status_code == 200
    assert account_response.json()["data"]["status"] == "active"


def test_same_account_active_sessions_reuse_same_node(client: TestClient) -> None:
    token = bootstrap(client)
    first_node_id, _ = create_node(client, token, name="us-west-1", weight=10)
    second_node_id, second_node_token = create_node(client, token, name="us-west-2", weight=100)
    device_id, device_token = register_device(client, token)
    first_workspace_id = create_workspace(client, device_token, device_id, "sha256:first")
    second_workspace_id = create_workspace(client, device_token, device_id, "sha256:second")
    account_id = create_account(client, token)

    async def seed_active_session() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            account = await session.get(ToolAccount, UUID(account_id))
            assert account is not None
            account.affinity_node_id = UUID(first_node_id)
            tool_session = Session(
                tool_type="claude",
                user_id=account.user_id,
                tool_account_id=account.id,
                workspace_id=UUID(first_workspace_id),
                node_id=UUID(first_node_id),
                project_key="sha256:first",
                status="running",
                tmux_session_name="ar-claude-existing",
                container_id="agent-remote-claude-existing",
            )
            session.add(tool_session)
            await session.commit()

    asyncio.run(seed_active_session())

    response = client.post(
        "/api/v1/sessions",
        headers=auth_header(token),
        json={
            "tool_type": "claude",
            "tool_account_id": account_id,
            "workspace_id": second_workspace_id,
            "project_key": "sha256:second",
            "argv": [],
        },
    )
    assert response.status_code == 200
    tool_session = response.json()["data"]
    assert tool_session["node_id"] == first_node_id
    assert tool_session["node_id"] != second_node_id

    poll_response = client.post(
        "/api/v1/node-api/tasks/poll", headers=auth_header(second_node_token)
    )
    assert poll_response.status_code == 200
    assert poll_response.json()["data"]["tasks"] == []


def test_native_reconcile_interrupts_and_replacement_is_explicit(client: TestClient) -> None:
    token = bootstrap(client)
    node_id, node_token = create_node(client, token, name="us-west-native", weight=10)
    device_id, device_token = register_device(client, token)
    workspace_id = create_workspace(client, device_token, device_id, "sha256:replacement")
    account_id = create_account(client, token)
    created = client.post(
        "/api/v1/sessions",
        headers=auth_header(token),
        json={
            "tool_type": "claude",
            "tool_account_id": account_id,
            "workspace_id": workspace_id,
            "project_key": "sha256:replacement",
            "argv": [],
        },
    )
    assert created.status_code == 200
    session_id = str(created.json()["data"]["id"])

    async def make_native_running() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            tool_session = await session.get(Session, UUID(session_id))
            account = await session.get(ToolAccount, UUID(account_id))
            node = await session.get(Node, UUID(node_id))
            assert tool_session is not None and account is not None and node is not None
            tool_session.status = "running"
            tool_session.runtime_backend = "native"
            tool_session.runtime_resource_id = "agent-remote-session-test.service"
            account.runtime_backend = "native"
            node.allowed_runtime_backends = ["docker_sandbox", "native"]
            node.runtime_capabilities = {"backends": ["docker_sandbox", "native"]}
            await session.commit()

    asyncio.run(make_native_running())
    reconciled = client.post(
        "/api/v1/node-api/reconcile",
        headers=auth_header(node_token),
        json={
            "node_id": node_id,
            "sections": ["runtime_sessions"],
            "snapshot": {"sessions": []},
        },
    )
    assert reconciled.status_code == 200
    interrupted = client.get(f"/api/v1/sessions/{session_id}", headers=auth_header(token))
    assert interrupted.status_code == 200
    assert interrupted.json()["data"]["status"] == "interrupted"
    replacement = client.post(
        "/api/v1/sessions",
        headers=auth_header(token),
        json={
            "tool_type": "claude",
            "tool_account_id": account_id,
            "workspace_id": workspace_id,
            "project_key": "sha256:replacement",
            "argv": [],
            "replaces_session_id": session_id,
        },
    )
    assert replacement.status_code == 200
    assert replacement.json()["data"]["replaces_session_id"] == session_id
