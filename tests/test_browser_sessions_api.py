"""
验证浏览器会话 API行为。
"""

import asyncio
from collections.abc import Iterator
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_remote_server.api import browser_sessions as browser_sessions_api
from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.main import create_app
from agent_remote_server.models import ToolAccount
from agent_remote_server.services.browser_sessions import BrowserSessionService


async def create_schema(app: FastAPI) -> None:
    """
    创建测试数据库 schema

    :param app (FastAPI): FastAPI 应用
    """

    async with app.state.database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """
    创建测试 API 客户端。

    :return Iterator[TestClient]: 测试 API 客户端
    """
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
    :return dict[str, str]: 请求头
    """

    return {"Authorization": f"Bearer {token}"}


def bootstrap(client: TestClient) -> str:
    """
    登录管理员测试账号。

    :param client (TestClient): 测试 API 客户端
    :return str: 管理员访问令牌
    """
    response = client.post(
        "/api/v1/auth/bootstrap",
        json={"username": "admin", "password": "admin-secret"},
    )
    assert response.status_code == 200
    return str(response.json()["data"]["access_token"])


def create_node(client: TestClient, token: str) -> tuple[str, str]:
    """
    创建节点。

    :param client (TestClient): 测试 API 客户端
    :param token (str): 令牌
    :return tuple[str, str]: 节点
    """
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
            "version": "0.2.16",
        },
    )
    assert register.status_code == 200
    return node_id, str(register.json()["data"]["node_token"])


def create_account(client: TestClient, token: str) -> str:
    """
    创建账号。

    :param client (TestClient): 测试 API 客户端
    :param token (str): 令牌
    :return str: 账号
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
        """
        激活目标记录。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            account = await session.get(ToolAccount, UUID(account_id))
            assert account is not None
            account.status = "active"
            await session.commit()

    asyncio.run(activate())
    return account_id


def test_browser_session_lifecycle(client: TestClient) -> None:
    """
    验证浏览器会话生命周期。

    :param client (TestClient): 测试 API 客户端
    """
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

    blocked_delete = client.delete(
        f"/api/v1/browser-sessions/{browser_session['id']}", headers=auth_header(token)
    )
    assert blocked_delete.status_code == 409
    assert blocked_delete.json()["error"]["code"] == "BROWSER_SESSION_DELETE_REQUIRES_STOPPED"

    complete_stop = client.post(
        f"/api/v1/node-api/tasks/{stop_task['task_id']}/complete",
        headers=auth_header(node_token),
        json={"result": {"status": "stopped", "browser_session_id": browser_session["id"]}},
    )
    assert complete_stop.status_code == 200
    deleted = client.delete(
        f"/api/v1/browser-sessions/{browser_session['id']}", headers=auth_header(token)
    )
    assert deleted.status_code == 200


def test_browser_stream_disables_upstream_ping(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    验证浏览器数据流禁用上游 Ping。

    :param client (TestClient): 测试 API 客户端
    :param monkeypatch (pytest.MonkeyPatch): pytest 补丁工具
    """
    connect_options: dict[str, Any] = {}

    async def fake_stream_endpoint(
        service: BrowserSessionService, *, browser_session_id: UUID, token: str
    ) -> str:
        """
        返回测试替身数据流端点。

        :param service (BrowserSessionService): 业务服务
        :param browser_session_id (UUID): 浏览器会话 ID
        :param token (str): 令牌
        :return str: 测试替身数据流端点
        """
        del service, browser_session_id, token
        return "https://kasm_user:secret@browser:6901/"

    class FakeUpstreamSocket:
        """
        定义测试替身上游套接字。
        """

        subprotocol = None

        def __aiter__(self) -> Any:
            """
            异步迭代当前集合。

            :return Any: 异步迭代
            """

            async def messages() -> Any:
                """
                返回消息。

                :return Any: 消息
                """
                if False:
                    yield b""

            return messages()

        async def close(self) -> None:
            """
            关闭当前连接。
            """
            return None

        async def send(self, message: bytes | str) -> None:
            """
            接收测试 WebSocket 发出的消息。

            :param message (bytes | str): 消息内容
            """
            del message

    class FakeConnection:
        """
        定义测试替身连接。
        """

        async def __aenter__(self) -> FakeUpstreamSocket:
            """
            进入异步上下文。

            :return FakeUpstreamSocket: 进入异步上下文
            """
            return FakeUpstreamSocket()

        async def __aexit__(self, *args: object) -> None:
            """
            退出异步上下文。

            :param args (object): 命令行位置参数
            """
            del args

    def fake_connect(url: str, **kwargs: Any) -> FakeConnection:
        """
        返回测试替身连接。

        :param url (str): 目标 URL
        :param kwargs (Any): 命令行关键字参数
        :return FakeConnection: 测试替身连接
        """
        connect_options.update(kwargs)
        assert url == "wss://browser:6901/websockify"
        return FakeConnection()

    monkeypatch.setattr(BrowserSessionService, "stream_endpoint", fake_stream_endpoint)
    monkeypatch.setattr(browser_sessions_api.websockets, "connect", fake_connect)

    browser_session_id = "d06d6e90-8486-4ffb-abdc-9117e6aaf651"
    with client.websocket_connect(
        f"/api/v1/browser-sessions/{browser_session_id}/stream/websockify?token=test-token"
    ):
        pass

    assert connect_options["ping_interval"] is None
