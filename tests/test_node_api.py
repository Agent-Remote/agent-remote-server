import asyncio
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
from agent_remote_server.models import AuditLog, Node, NodeTaskResult
from agent_remote_server.services.nodes import NodeService


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


def create_and_register_node(client: TestClient, admin_token: str) -> tuple[str, str]:
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
        },
    )
    assert create_response.status_code == 200
    create_payload = create_response.json()["data"]
    node_id = str(create_payload["node"]["id"])
    registration_token = str(create_payload["registration_token"])

    register_response = client.post(
        "/api/v1/node-api/register",
        json={
            "node_id": node_id,
            "registration_token": registration_token,
            "version": "0.0.4+fix.3",
        },
    )
    assert register_response.status_code == 200
    return node_id, str(register_response.json()["data"]["node_token"])


def heartbeat_payload(
    node_id: str, *, docker_ok: bool = True, tmux_ok: bool = True
) -> dict[str, object]:
    """
    创建心跳 payload

    :param node_id (str): 节点 ID
    :param docker_ok (bool): Docker 是否可用
    :param tmux_ok (bool): Tmux 是否可用

    :return dict: 心跳 payload
    """

    return {
        "node_id": node_id,
        "version": "0.0.4+fix.3",
        "supported_tool_types": ["claude"],
        "wireguard_ip": "10.77.0.1",
        "wireguard_public_key": "node-wireguard-public-key",
        "wireguard_endpoint": "203.0.113.10:51820",
        "resources": {
            "cpu_load": 0.1,
            "memory_used_bytes": 1024,
            "memory_total_bytes": 2048,
            "disk_used_bytes": 4096,
            "disk_total_bytes": 8192,
        },
        "runtime": {
            "docker_ok": docker_ok,
            "tmux_ok": tmux_ok,
            "active_sessions": 0,
            "active_browser_sessions": 0,
            "containers": 0,
        },
    }


def test_node_registration_heartbeat_and_offline_marking(client: TestClient) -> None:
    admin_token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, admin_token)

    heartbeat_response = client.post(
        "/api/v1/node-api/heartbeat",
        headers=auth_header(node_token),
        json=heartbeat_payload(node_id),
    )
    assert heartbeat_response.status_code == 200

    get_response = client.get(f"/api/v1/nodes/{node_id}", headers=auth_header(admin_token))
    assert get_response.status_code == 200
    assert get_response.json()["data"]["status"] == "healthy"
    assert get_response.json()["data"]["wireguard_ip"] == "10.77.0.1"
    assert get_response.json()["data"]["wireguard_public_key"] == "node-wireguard-public-key"
    assert get_response.json()["data"]["wireguard_endpoint"] == "203.0.113.10:51820"

    register_device = client.post(
        "/api/v1/devices/register",
        headers=auth_header(admin_token),
        json={
            "name": "macbook",
            "platform": "macos",
            "ssh_public_key": "ssh-ed25519 AAAATEST rem@test",
            "wireguard_public_key": "device-wireguard-public-key",
        },
    )
    assert register_device.status_code == 200
    peer_response = client.get("/api/v1/node-api/wireguard/peers", headers=auth_header(node_token))
    assert peer_response.status_code == 200
    assert peer_response.json()["data"]["items"] == [
        {
            "public_key": "device-wireguard-public-key",
            "allowed_ips": ["10.77.0.2/32"],
        }
    ]

    async def make_stale() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            node = await session.get(Node, UUID(node_id))
            assert node is not None
            node.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
            await session.commit()

    asyncio.run(make_stale())

    stale_response = client.get(f"/api/v1/nodes/{node_id}", headers=auth_header(admin_token))
    assert stale_response.status_code == 200
    assert stale_response.json()["data"]["status"] == "offline"


def test_node_task_lease_and_idempotent_completion(client: TestClient) -> None:
    admin_token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, admin_token)

    async def create_task() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            await NodeService(session, app.state.settings).create_task(
                node_id=UUID(node_id),
                task_id="task_test",
                task_type="reconcile_state",
                payload={"target": "node"},
            )
            duplicate = await NodeService(session, app.state.settings).create_task(
                node_id=UUID(node_id),
                task_id="task_test",
                task_type="reconcile_state",
                payload={"target": "duplicate"},
            )
            assert duplicate.payload == {"target": "node"}

    asyncio.run(create_task())

    poll_response = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert poll_response.status_code == 200
    tasks = poll_response.json()["data"]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "task_test"
    assert tasks[0]["lease_until"]

    start_response = client.post(
        "/api/v1/node-api/tasks/task_test/start",
        headers=auth_header(node_token),
    )
    assert start_response.status_code == 200

    complete_response = client.post(
        "/api/v1/node-api/tasks/task_test/complete",
        headers=auth_header(node_token),
        json={"result": {"ok": True}},
    )
    assert complete_response.status_code == 200

    duplicate_complete = client.post(
        "/api/v1/node-api/tasks/task_test/complete",
        headers=auth_header(node_token),
        json={"result": {"ok": True}},
    )
    assert duplicate_complete.status_code == 200

    async def count_results() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            results = list(await session.scalars(select(NodeTaskResult)))
            assert len(results) == 1
            assert results[0].status == "succeeded"

    asyncio.run(count_results())


def test_admin_can_list_failed_node_tasks(client: TestClient) -> None:
    admin_token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, admin_token)

    async def create_task() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            await NodeService(session, app.state.settings).create_task(
                node_id=UUID(node_id),
                task_id="task_failed",
                task_type="create_tool_session",
                payload={"session_id": "session-test"},
            )

    asyncio.run(create_task())

    poll_response = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert poll_response.status_code == 200

    fail_response = client.post(
        "/api/v1/node-api/tasks/task_failed/fail",
        headers=auth_header(node_token),
        json={"error": {"code": "docker_create_failed", "message": "failed to create container"}},
    )
    assert fail_response.status_code == 200

    list_response = client.get(
        "/api/v1/nodes/tasks?status=failed",
        headers=auth_header(admin_token),
    )
    assert list_response.status_code == 200
    tasks = list_response.json()["data"]["items"]
    assert [task["task_id"] for task in tasks] == ["task_failed"]
    assert tasks[0]["result"]["error"]["code"] == "docker_create_failed"

    get_response = client.get(
        "/api/v1/nodes/tasks/task_failed",
        headers=auth_header(admin_token),
    )
    assert get_response.status_code == 200
    assert get_response.json()["data"]["status"] == "failed"


def test_node_reconcile_and_disable(client: TestClient) -> None:
    admin_token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, admin_token)

    reconcile_response = client.post(
        "/api/v1/node-api/reconcile",
        headers=auth_header(node_token),
        json={
            "node_id": node_id,
            "sections": ["sessions", "containers"],
            "snapshot": {"containers": []},
        },
    )
    assert reconcile_response.status_code == 200

    disable_response = client.post(
        f"/api/v1/nodes/{node_id}/disable", headers=auth_header(admin_token)
    )
    assert disable_response.status_code == 200
    assert disable_response.json()["data"]["status"] == "disabled"

    heartbeat_response = client.post(
        "/api/v1/node-api/heartbeat",
        headers=auth_header(node_token),
        json=heartbeat_payload(node_id),
    )
    assert heartbeat_response.status_code == 401

    async def inspect_audit() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            logs = list(await session.scalars(select(AuditLog)))
            actions = {log.action for log in logs}
            assert "node_api.reconcile" in actions

    asyncio.run(inspect_audit())
