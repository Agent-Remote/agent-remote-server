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
from agent_remote_server.models import ToolAccount


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
    response = client.post(
        "/api/v1/auth/bootstrap",
        json={"username": "admin", "password": "admin-secret"},
    )
    assert response.status_code == 200
    return str(response.json()["data"]["access_token"])


def create_node(client: TestClient, token: str) -> tuple[str, str]:
    response = client.post(
        "/api/v1/nodes",
        headers=auth_header(token),
        json={
            "name": "us-west-1",
            "region_code": "US",
            "tags": ["us"],
            "weight": 10,
            "supported_tool_types": ["claude"],
            "wireguard_ip": "10.42.0.10",
            "ssh_host": "10.42.0.10",
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
            "version": "0.0.4",
        },
    )
    assert register.status_code == 200
    return node_id, str(register.json()["data"]["node_token"])


def create_account(client: TestClient, token: str) -> str:
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


def test_browser_session_lifecycle(client: TestClient) -> None:
    token = bootstrap(client)
    node_id, node_token = create_node(client, token)
    account_id = create_account(client, token)

    response = client.post(
        "/api/v1/browser-sessions",
        headers=auth_header(token),
        json={
            "tool_account_id": account_id,
            "target_url": "https://claude.ai",
            "ttl_seconds": 900,
        },
    )
    assert response.status_code == 200
    browser_session = response.json()["data"]
    assert browser_session["status"] == "starting"
    assert browser_session["node_id"] == node_id
    assert browser_session["region_code"] == "US"
    assert browser_session["timezone"] == "America/Los_Angeles"
    assert browser_session["locale"] == "en_US.UTF-8"
    assert browser_session["container_id"].startswith("agent-remote-browser-")

    poll_response = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert poll_response.status_code == 200
    tasks = poll_response.json()["data"]["tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["task_type"] == "create_browser_session"
    assert task["payload"]["browser_session_id"] == browser_session["id"]
    assert task["payload"]["tool_account_id"] == account_id
    assert task["payload"]["target_url"] == "https://claude.ai/"
    assert task["payload"]["browser"]["mode"] == "incognito"
    assert task["payload"]["network_policy"]["deny_metadata_service"] is True

    complete_response = client.post(
        f"/api/v1/node-api/tasks/{task['task_id']}/complete",
        headers=auth_header(node_token),
        json={
            "result": {
                "status": "ready",
                "browser_session_id": browser_session["id"],
                "container_id": task["payload"]["container_name"],
                "stream_endpoint": f"node-local://browser/{browser_session['id']}",
            }
        },
    )
    assert complete_response.status_code == 200

    connect_response = client.post(
        f"/api/v1/browser-sessions/{browser_session['id']}/connect-info",
        headers=auth_header(token),
    )
    assert connect_response.status_code == 200
    connect_info = connect_response.json()["data"]
    assert connect_info["status"] == "ready"
    assert connect_info["embed_url"].startswith(
        f"/api/v1/browser-sessions/{browser_session['id']}/stream?token=bembed_"
    )

    stop_response = client.post(
        f"/api/v1/browser-sessions/{browser_session['id']}/stop",
        headers=auth_header(token),
    )
    assert stop_response.status_code == 200

    stop_poll = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert stop_poll.status_code == 200
    stop_task = stop_poll.json()["data"]["tasks"][0]
    assert stop_task["task_type"] == "stop_browser_session"
    assert stop_task["payload"]["browser_session_id"] == browser_session["id"]
