import asyncio
import base64
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.main import create_app


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
            "wireguard_ip": "10.42.0.10",
            "ssh_host": "10.42.0.10",
            "ssh_port": 22,
            "ssh_user": "agent-remote",
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
            "version": "0.0.3",
        },
    )
    assert register_response.status_code == 200
    return node_id, str(register_response.json()["data"]["node_token"])


def create_tool_account(
    client: TestClient,
    token: str,
    *,
    name: str = "Claude US",
    tool_type: str = "claude",
) -> dict[str, object]:
    """
    创建测试工具账户

    :param client (TestClient): 测试客户端
    :param token (str): 用户令牌
    :param name (str): 显示名称
    :param tool_type (str): 工具类型

    :return dict: 工具账户数据
    """

    response = client.post(
        "/api/v1/tool-accounts",
        headers=auth_header(token),
        json={
            "tool_type": tool_type,
            "display_name": name,
            "region_code": "US",
            "timezone": "America/Los_Angeles",
            "locale": "en_US.UTF-8",
            "preferred_node_tags": ["us"],
        },
    )
    assert response.status_code == 200
    return dict(response.json()["data"])


def test_tool_account_create_list_and_validation(client: TestClient) -> None:
    token = bootstrap(client)

    first = create_tool_account(client, token, name="Claude Main")
    second = create_tool_account(client, token, name="Claude Alt")

    assert first["tool_type"] == "claude"
    assert first["status"] == "binding_requested"
    assert first["affinity_node_id"] is None
    assert second["id"] != first["id"]

    list_response = client.get("/api/v1/tool-accounts", headers=auth_header(token))
    assert list_response.status_code == 200
    accounts = list_response.json()["data"]["items"]
    assert [account["display_name"] for account in accounts] == ["Claude Main", "Claude Alt"]

    unsupported = client.post(
        "/api/v1/tool-accounts",
        headers=auth_header(token),
        json={
            "tool_type": "unknown",
            "display_name": "Unknown",
            "region_code": "US",
            "timezone": "America/Los_Angeles",
            "locale": "en_US.UTF-8",
            "preferred_node_tags": [],
        },
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "TOOL_TYPE_UNSUPPORTED"


def test_tool_account_binding_task_and_status_update(client: TestClient) -> None:
    token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, token)
    account = create_tool_account(client, token)

    start_response = client.post(
        f"/api/v1/tool-accounts/{account['id']}/bind/start",
        headers=auth_header(token),
    )
    assert start_response.status_code == 200
    binding = start_response.json()["data"]
    assert binding["status"] == "binding_session_starting"
    assert binding["node_id"] == node_id
    assert binding["task_id"] == f"create_binding_session:{account['id']}"
    assert binding["account_remote_path"].endswith(f"/tool-accounts/claude/{account['id']}")

    poll_response = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert poll_response.status_code == 200
    tasks = poll_response.json()["data"]["tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["task_type"] == "create_binding_session"
    assert task["payload"]["tool_account_id"] == account["id"]
    assert task["payload"]["template"]["sandbox_agent"] == "claude"
    assert task["payload"]["template"]["command"] == ["claude", "login"]
    assert task["payload"]["timezone"] == "America/Los_Angeles"
    assert task["payload"]["locale"] == "en_US.UTF-8"

    complete_response = client.post(
        f"/api/v1/node-api/tasks/{task['task_id']}/complete",
        headers=auth_header(node_token),
        json={
            "result": {
                "status": "waiting_user_login",
                "binding_session_id": "bind-test",
                "tmux_session_name": "bind-claude",
                "account_remote_path": task["payload"]["account_remote_path"],
            }
        },
    )
    assert complete_response.status_code == 200

    status_response = client.get(
        f"/api/v1/tool-accounts/{account['id']}/bind/status",
        headers=auth_header(token),
    )
    assert status_response.status_code == 200
    status = status_response.json()["data"]
    assert status["status"] == "binding_waiting_user_login"
    assert status["binding_session_id"] == "bind-test"
    assert "tmux attach-session -t bind-claude" in status["connect_command"]


def test_tool_account_verifier_success_makes_account_active(client: TestClient) -> None:
    token = bootstrap(client)
    _node_id, node_token = create_and_register_node(client, token)
    account = create_tool_account(client, token)

    start_response = client.post(
        f"/api/v1/tool-accounts/{account['id']}/bind/start",
        headers=auth_header(token),
    )
    assert start_response.status_code == 200
    poll_bind = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    bind_task = poll_bind.json()["data"]["tasks"][0]
    complete_bind = client.post(
        f"/api/v1/node-api/tasks/{bind_task['task_id']}/complete",
        headers=auth_header(node_token),
        json={
            "result": {
                "status": "waiting_user_login",
                "binding_session_id": "bind-test",
                "tmux_session_name": "bind-claude",
                "account_remote_path": bind_task["payload"]["account_remote_path"],
            }
        },
    )
    assert complete_bind.status_code == 200

    verify_response = client.post(
        f"/api/v1/tool-accounts/{account['id']}/bind/verify",
        headers=auth_header(token),
    )
    assert verify_response.status_code == 200
    assert verify_response.json()["data"]["status"] == "binding_verifying"

    poll_verify = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    verify_tasks = poll_verify.json()["data"]["tasks"]
    assert len(verify_tasks) == 1
    verify_task = verify_tasks[0]
    assert verify_task["task_type"] == "verify_tool_account"
    assert verify_task["payload"]["verifier"] == "claude"

    complete_verify = client.post(
        f"/api/v1/node-api/tasks/{verify_task['task_id']}/complete",
        headers=auth_header(node_token),
        json={
            "result": {
                "verified": True,
                "account_remote_path": verify_task["payload"]["account_remote_path"],
                "metadata": {"matched_paths": [".agent-remote-claude-auth.json"]},
            }
        },
    )
    assert complete_verify.status_code == 200

    get_response = client.get(
        f"/api/v1/tool-accounts/{account['id']}",
        headers=auth_header(token),
    )
    assert get_response.status_code == 200
    assert get_response.json()["data"]["status"] == "active"


def test_tool_account_config_import_creates_node_task(client: TestClient) -> None:
    token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, token)
    account = create_tool_account(client, token)

    content = base64.b64encode(b'{"theme":"dark"}\n').decode("ascii")
    response = client.post(
        f"/api/v1/tool-accounts/{account['id']}/config-imports",
        headers=auth_header(token),
        json={
            "tool_type": "claude",
            "source": "local_cli",
            "include": ["~/.claude/settings.json"],
            "exclude": [],
            "files": [
                {
                    "path": "~/.claude/settings.json",
                    "content_base64": content,
                    "mode": 384,
                }
            ],
            "include_resume_history": False,
            "dry_run": False,
        },
    )
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["accepted"] == ["~/.claude/settings.json"]
    assert payload["rejected"] == []
    assert payload["task_id"].startswith(f"import_tool_account_config:{account['id']}:")
    assert payload["account_remote_path"].endswith(f"/tool-accounts/claude/{account['id']}")
    assert payload["imported_file_count"] == 1

    poll_response = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert poll_response.status_code == 200
    tasks = poll_response.json()["data"]["tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["node_id"] == node_id
    assert task["task_type"] == "import_tool_account_config"
    assert task["payload"]["tool_account_id"] == account["id"]
    assert task["payload"]["files"][0]["path"] == "~/.claude/settings.json"
