import asyncio
import base64
from collections.abc import Iterator
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.main import create_app
from agent_remote_server.models import Node, ToolAccount


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
            "version": "0.0.4+fix.7",
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
    policy = {
        "memory_high_bytes": 512 * 1024 * 1024,
        "memory_max_bytes": 768 * 1024 * 1024,
        "cpu_quota_percent": 100,
    }
    update_response = client.patch(
        f"/api/v1/nodes/{node_id}",
        headers=auth_header(token),
        json={"runtime_policy": policy},
    )
    assert update_response.status_code == 200
    account = create_tool_account(client, token)

    start_response = client.post(
        f"/api/v1/tool-accounts/{account['id']}/bind/start",
        headers=auth_header(token),
    )
    assert start_response.status_code == 200
    binding = start_response.json()["data"]
    assert binding["status"] == "binding_session_starting"
    assert binding["node_id"] == node_id
    assert binding["task_id"].startswith(f"create_binding_session:{account['id']}:")
    assert len(binding["binding_session_id"]) <= 32
    assert binding["account_remote_path"].endswith(f"/tool-accounts/claude/{account['id']}")

    poll_response = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert poll_response.status_code == 200
    tasks = poll_response.json()["data"]["tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["task_type"] == "create_binding_session"
    assert task["payload"]["tool_account_id"] == account["id"]
    assert task["payload"]["template"]["sandbox_agent"] == "claude"
    assert task["payload"]["template"]["command"] == ["claude", "auth", "login"]
    assert task["payload"]["timezone"] == "America/Los_Angeles"
    assert task["payload"]["locale"] == "en_US.UTF-8"
    assert task["payload"]["runtime_policy"] == policy

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
    assert status["connect_command"].startswith("ssh -t -p 22 ")
    assert "tmux attach-session -t bind-claude" in status["connect_command"]

    retry_response = client.post(
        f"/api/v1/tool-accounts/{account['id']}/bind/start",
        headers=auth_header(token),
    )
    assert retry_response.status_code == 200
    retry = retry_response.json()["data"]
    assert retry["status"] == "binding_session_starting"
    assert retry["task_id"] != task["task_id"]
    assert retry["binding_session_id"] != task["payload"]["binding_id"]
    assert retry["tmux_session_name"] != task["payload"]["tmux_session_name"]

    retry_poll = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert retry_poll.status_code == 200
    retry_tasks = retry_poll.json()["data"]["tasks"]
    assert len(retry_tasks) == 1
    assert retry_tasks[0]["task_id"] == retry["task_id"]

    stale_failure = client.post(
        f"/api/v1/node-api/tasks/{task['task_id']}/fail",
        headers=auth_header(node_token),
        json={"error": {"code": "RUNTIME_FAILED", "message": "stale failure"}},
    )
    assert stale_failure.status_code == 200

    retry_complete = client.post(
        f"/api/v1/node-api/tasks/{retry_tasks[0]['task_id']}/complete",
        headers=auth_header(node_token),
        json={
            "result": {
                "status": "waiting_user_login",
                "binding_session_id": retry["binding_session_id"],
                "tmux_session_name": retry["tmux_session_name"],
                "account_remote_path": retry["account_remote_path"],
            }
        },
    )
    assert retry_complete.status_code == 200

    retry_status = client.get(
        f"/api/v1/tool-accounts/{account['id']}/bind/status",
        headers=auth_header(token),
    )
    assert retry_status.status_code == 200
    assert retry_status.json()["data"]["status"] == "binding_waiting_user_login"
    assert retry_status.json()["data"]["error"] is None


def test_native_binding_requires_device_and_syncs_forced_command_key(
    client: TestClient,
) -> None:
    admin_token = bootstrap(client)
    create_response = client.post(
        "/api/v1/nodes",
        headers=auth_header(admin_token),
        json={
            "name": "native-us-west",
            "region_code": "US",
            "tags": ["us"],
            "supported_tool_types": ["claude"],
            "allowed_runtime_backends": ["native"],
            "default_runtime_backend": "native",
            "ssh_host": "10.42.0.20",
            "ssh_user": "agent-remote",
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()["data"]
    node_id = created["node"]["id"]
    register = client.post(
        "/api/v1/node-api/register",
        json={
            "node_id": node_id,
            "registration_token": created["registration_token"],
            "version": "0.0.4+fix.7",
        },
    )
    assert register.status_code == 200
    node_token = register.json()["data"]["node_token"]
    heartbeat = client.post(
        "/api/v1/node-api/heartbeat",
        headers=auth_header(node_token),
        json={
            "node_id": node_id,
            "version": "0.0.4+fix.7",
            "supported_tool_types": ["claude"],
            "resources": {
                "cpu_load": 0.1,
                "memory_used_bytes": 1024,
                "memory_total_bytes": 4096,
                "disk_used_bytes": 1024,
                "disk_total_bytes": 8192,
            },
            "runtime": {
                "docker_ok": False,
                "tmux_ok": True,
                "runtime_capabilities": {
                    "backends": ["native"],
                    "native": {"linux": True},
                },
            },
        },
    )
    assert heartbeat.status_code == 200

    device_response = client.post(
        "/api/v1/devices/register",
        headers=auth_header(admin_token),
        json={
            "name": "native-client",
            "platform": "macos",
            "ssh_public_key": "ssh-ed25519 AAAANATIVE rem@test",
            "wireguard_public_key": "native-device-wg",
        },
    )
    assert device_response.status_code == 200
    device = device_response.json()["data"]
    device_id = device["device"]["id"]
    device_token = device["device_token"]["access_token"]
    account = create_tool_account(client, device_token)

    start = client.post(
        f"/api/v1/tool-accounts/{account['id']}/bind/start",
        headers=auth_header(device_token),
    )
    assert start.status_code == 200
    binding = start.json()["data"]
    assert binding["runtime_backend"] == "native"
    assert binding["connect_command"].startswith("ssh -t -p 22 ")
    assert f"agent-remote-attach --binding {account['id']}" in binding["connect_command"]

    task_types: set[str] = set()
    forced_commands: list[str] = []
    for _ in range(2):
        poll = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
        assert poll.status_code == 200
        tasks = poll.json()["data"]["tasks"]
        assert len(tasks) == 1
        task_types.add(tasks[0]["task_type"])
        if tasks[0]["task_type"] == "sync_ssh_keys":
            forced_commands.extend(key["forced_command"] for key in tasks[0]["payload"]["ssh_keys"])
    assert task_types == {"create_binding_session", "sync_ssh_keys"}
    assert forced_commands == [f"agent-remote-attach --device {device_id}"]


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


def test_runtime_migration_commits_only_after_task_success(client: TestClient) -> None:
    token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, token)
    account = create_tool_account(client, token)
    started = client.post(
        f"/api/v1/tool-accounts/{account['id']}/bind/start", headers=auth_header(token)
    )
    assert started.status_code == 200
    binding_task = client.post(
        "/api/v1/node-api/tasks/poll", headers=auth_header(node_token)
    ).json()["data"]["tasks"][0]
    completed = client.post(
        f"/api/v1/node-api/tasks/{binding_task['task_id']}/complete",
        headers=auth_header(node_token),
        json={
            "result": {
                "status": "waiting_user_login",
                "runtime_backend": "docker_sandbox",
                "account_remote_path": binding_task["payload"]["account_remote_path"],
            }
        },
    )
    assert completed.status_code == 200

    async def enable_native() -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            node = await session.get(Node, UUID(node_id))
            tool_account = await session.get(ToolAccount, UUID(str(account["id"])))
            assert node is not None and tool_account is not None
            node.allowed_runtime_backends = ["docker_sandbox", "native"]
            node.runtime_capabilities = {"backends": ["docker_sandbox", "native"]}
            tool_account.status = "active"
            await session.commit()

    asyncio.run(enable_native())
    migration = client.post(
        f"/api/v1/tool-accounts/{account['id']}/runtime-migration",
        headers=auth_header(token),
        json={"target_runtime_backend": "native"},
    )
    assert migration.status_code == 200
    migration_data = migration.json()["data"]
    assert migration_data["source_runtime_backend"] == "docker_sandbox"
    task = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token)).json()[
        "data"
    ]["tasks"][0]
    assert task["task_type"] == "migrate_tool_account_runtime"
    succeeded = client.post(
        f"/api/v1/node-api/tasks/{task['task_id']}/complete",
        headers=auth_header(node_token),
        json={
            "result": {
                "migrated": True,
                "runtime_backend": "native",
                "backup_path": "/var/lib/agent-remote-runtime/migrations/test",
            }
        },
    )
    assert succeeded.status_code == 200
    current = client.get(
        f"/api/v1/tool-accounts/{account['id']}", headers=auth_header(token)
    ).json()["data"]
    assert current["runtime_backend"] == "native"
    assert current["status"] == "active"

    rollback = client.post(
        f"/api/v1/tool-accounts/{account['id']}/runtime-migration",
        headers=auth_header(token),
        json={"target_runtime_backend": "docker_sandbox"},
    )
    assert rollback.status_code == 200
    rollback_task = client.post(
        "/api/v1/node-api/tasks/poll", headers=auth_header(node_token)
    ).json()["data"]["tasks"][0]
    failed = client.post(
        f"/api/v1/node-api/tasks/{rollback_task['task_id']}/fail",
        headers=auth_header(node_token),
        json={"error": {"code": "RUNTIME_FAILED", "message": "verification failed"}},
    )
    assert failed.status_code == 200
    after_failure = client.get(
        f"/api/v1/tool-accounts/{account['id']}", headers=auth_header(token)
    ).json()["data"]
    assert after_failure["runtime_backend"] == "native"
    assert after_failure["status"] == "active"
