"""
验证节点 API行为。
"""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.ego_browser.release_policy import EGO_BROWSER_WRAPPER_VERSION
from agent_remote_server.main import create_app
from agent_remote_server.models import AuditLog, DeviceSession, Node, NodeTask, NodeTaskResult
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


def create_user(client: TestClient, admin_token: str, username: str) -> str:
    """
    创建普通用户并返回其访问 token。

    :param client (TestClient): 测试 API 客户端
    :param admin_token (str): 管理员令牌
    :param username (str): 用户名
    :return str: 用户
    """

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
    return str(logged_in.json()["data"]["access_token"])


def create_and_register_node(client: TestClient, admin_token: str) -> tuple[str, str]:
    """
    创建并注册节点

    :param client (TestClient): 测试客户端
    :param admin_token (str): 管理员令牌
    :return tuple[str, str]: 节点 ID 和 node token
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
            "version": "0.2.16",
        },
    )
    assert register_response.status_code == 200
    return node_id, str(register_response.json()["data"]["node_token"])


def test_node_join_code_http_contract_is_admin_only_and_secret_free(
    client: TestClient,
) -> None:
    """
    加入码 HTTP 接口返回确定状态，且审计不记录 code 或 Node token。

    :param client (TestClient): 测试 API 客户端
    """

    admin_token = bootstrap(client)
    user_token = create_user(client, admin_token, "join-code-user")
    node_id, _ = create_and_register_node(client, admin_token)
    revoked_exchange = "http-revoked-exchange-0001"
    consumed_exchange = "http-consumed-exchange-001"
    missing_exchange = "http-missing-exchange-00001"

    forbidden_issue = client.post(
        f"/api/v1/nodes/{node_id}/join-code",
        headers=auth_header(user_token),
        json={"exchange_id": revoked_exchange},
    )
    assert forbidden_issue.status_code == 403
    assert forbidden_issue.json()["error"]["code"] == "COMMON_FORBIDDEN"
    forbidden_revoke = client.post(
        f"/api/v1/nodes/{node_id}/join-code/revoke",
        headers=auth_header(user_token),
        json={"exchange_id": revoked_exchange},
    )
    assert forbidden_revoke.status_code == 403
    assert forbidden_revoke.json()["error"]["code"] == "COMMON_FORBIDDEN"

    revoked_issue = client.post(
        f"/api/v1/nodes/{node_id}/join-code",
        headers=auth_header(admin_token),
        json={"exchange_id": revoked_exchange, "ego_browser_enabled": False},
    )
    assert revoked_issue.status_code == 200, revoked_issue.text
    revoked_data = revoked_issue.json()["data"]
    assert set(revoked_data) == {
        "node_id",
        "code",
        "expires_at",
        "ego_browser_enabled",
    }
    assert revoked_data["node_id"] == node_id
    assert revoked_data["ego_browser_enabled"] is False
    revoked_code = str(revoked_data["code"])

    revoked = client.post(
        f"/api/v1/nodes/{node_id}/join-code/revoke",
        headers=auth_header(admin_token),
        json={"exchange_id": revoked_exchange},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"] == {"state": "revoked"}

    consumed_issue = client.post(
        f"/api/v1/nodes/{node_id}/join-code",
        headers=auth_header(admin_token),
        json={"exchange_id": consumed_exchange},
    )
    assert consumed_issue.status_code == 200, consumed_issue.text
    consumed_code = str(consumed_issue.json()["data"]["code"])
    exchanged = client.post(
        "/api/v1/node-api/join-code/exchange",
        json={
            "node_id": node_id,
            "version": "0.2.16",
            "join_code": consumed_code,
            "exchange_id": consumed_exchange,
            "wrapper_version": EGO_BROWSER_WRAPPER_VERSION,
            "skill_version": "2.0.0",
            "artifact_digest": (
                "sha256:a45cc7fcbea45a6f6222faf83c891b0fd22955193699dd99c9e40b0c0b4a0741"
            ),
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    node_token = str(exchanged.json()["data"]["node_token"])

    for exchange_id, expected_state in (
        (consumed_exchange, "consumed"),
        (missing_exchange, "missing"),
    ):
        response = client.post(
            f"/api/v1/nodes/{node_id}/join-code/revoke",
            headers=auth_header(admin_token),
            json={"exchange_id": exchange_id},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"state": expected_state}

    async def inspect_audit() -> None:
        """
        检查审计。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            logs = list(await session.scalars(select(AuditLog)))
            rendered = json.dumps([log.details for log in logs], sort_keys=True)
            assert revoked_exchange in rendered
            assert consumed_exchange in rendered
            assert revoked_code not in rendered
            assert consumed_code not in rendered
            assert node_token not in rendered

    asyncio.run(inspect_audit())


def heartbeat_payload(
    node_id: str, *, docker_ok: bool = True, tmux_ok: bool = True
) -> dict[str, object]:
    """
    创建心跳 payload

    :param node_id (str): 节点 ID
    :param docker_ok (bool): Docker 是否可用
    :param tmux_ok (bool): Tmux 是否可用
    :return dict[str, object]: 心跳 payload
    """

    return {
        "node_id": node_id,
        "version": "0.2.16",
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
    """
    验证节点注册心跳并离线标记。

    :param client (TestClient): 测试 API 客户端
    """
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
        """
        设置过期。
        """
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
    """
    验证节点任务租约并幂等完成结果。

    :param client (TestClient): 测试 API 客户端
    """
    admin_token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, admin_token)

    async def create_task() -> None:
        """
        创建任务。
        """
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
        """
        统计results。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            results = list(await session.scalars(select(NodeTaskResult)))
            assert len(results) == 1
            assert results[0].status == "succeeded"

    asyncio.run(count_results())


def test_ego_browser_cancel_task_results_are_content_free(client: TestClient) -> None:
    """
    Browser 取消任务只持久化严格协议结果或固定失败信息。

    :param client (TestClient): 测试 API 客户端
    """

    admin_token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, admin_token)
    completion_task_id = "cancel_ego_browser_request:content-safe-completion"
    failure_task_id = "cancel_ego_browser_request:content-safe-failure"

    async def create_tasks() -> None:
        """
        创建任务。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            service = NodeService(session, app.state.settings)
            for task_id, request_id in (
                (completion_task_id, "content-safe-completion"),
                (failure_task_id, "content-safe-failure"),
            ):
                await service.create_task(
                    node_id=UUID(node_id),
                    task_id=task_id,
                    task_type="cancel_ego_browser_request",
                    payload={
                        "binding_id": str(uuid4()),
                        "generation": 1,
                        "request_id": request_id,
                        "sequence": 1,
                    },
                )

    asyncio.run(create_tasks())

    completed = client.post(
        f"/api/v1/node-api/tasks/{completion_task_id}/complete",
        headers=auth_header(node_token),
        json={
            "result": {
                "status": "cancellation_completed",
                "request_active": False,
                "server_terminal_observed": False,
            }
        },
    )
    assert completed.status_code == 200, completed.text
    failed = client.post(
        f"/api/v1/node-api/tasks/{failure_task_id}/fail",
        headers=auth_header(node_token),
        json={"error": {"message": "sensitive-browser-content", "page": "secret"}},
    )
    assert failed.status_code == 200, failed.text

    async def verify_results() -> None:
        """
        验证任务结果。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            completion = await session.scalar(
                select(NodeTaskResult).where(NodeTaskResult.task_id == completion_task_id)
            )
            failure = await session.scalar(
                select(NodeTaskResult).where(NodeTaskResult.task_id == failure_task_id)
            )
            assert completion is not None
            assert completion.result == {
                "status": "cancellation_completed",
                "request_active": False,
                "server_terminal_observed": False,
            }
            assert completion.error is None
            assert failure is not None
            assert failure.result is None
            assert failure.error == {
                "code": "EGO_BROWSER_CANCELLATION_FAILED",
                "message": "Cancellation could not be confirmed.",
            }
            assert "sensitive-browser-content" not in str(failure.error)

    asyncio.run(verify_results())


def test_expired_running_node_task_is_released(client: TestClient) -> None:
    """
    验证过期状态运行中节点任务为释放。

    :param client (TestClient): 测试 API 客户端
    """
    admin_token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, admin_token)

    async def create_task() -> None:
        """
        创建任务。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            await NodeService(session, app.state.settings).create_task(
                node_id=UUID(node_id),
                task_id="task_expired_running",
                task_type="reconcile_state",
                payload={"target": "node"},
            )

    asyncio.run(create_task())

    first_poll = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert first_poll.status_code == 200
    assert [task["task_id"] for task in first_poll.json()["data"]["tasks"]] == [
        "task_expired_running"
    ]

    start_response = client.post(
        "/api/v1/node-api/tasks/task_expired_running/start",
        headers=auth_header(node_token),
    )
    assert start_response.status_code == 200

    async def expire_lease() -> None:
        """
        过期处理租约。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            task = await session.scalar(
                select(NodeTask).where(NodeTask.task_id == "task_expired_running")
            )
            assert task is not None
            assert task.status == "running"
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire_lease())

    second_poll = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert second_poll.status_code == 200
    tasks = second_poll.json()["data"]["tasks"]
    assert [task["task_id"] for task in tasks] == ["task_expired_running"]

    async def assert_released() -> None:
        """
        断言释放。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            task = await session.scalar(
                select(NodeTask).where(NodeTask.task_id == "task_expired_running")
            )
            assert task is not None
            assert task.status == "leased"
            assert task.retry_count == 2

    asyncio.run(assert_released())


def test_admin_can_list_failed_node_tasks(client: TestClient) -> None:
    """
    验证管理员可列出失败节点任务。

    :param client (TestClient): 测试 API 客户端
    """
    admin_token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, admin_token)

    async def create_task() -> None:
        """
        创建任务。
        """
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
    """
    验证节点协调并禁用。

    :param client (TestClient): 测试 API 客户端
    """
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
        """
        检查审计。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            logs = list(await session.scalars(select(AuditLog)))
            actions = {log.action for log in logs}
            assert "node_api.reconcile" in actions

    asyncio.run(inspect_audit())


def test_node_delete_requires_disabled_unreferenced_node(client: TestClient) -> None:
    """
    验证节点删除要求禁用状态未引用节点。

    :param client (TestClient): 测试 API 客户端
    """
    admin_token = bootstrap(client)
    created = client.post(
        "/api/v1/nodes",
        headers=auth_header(admin_token),
        json={
            "name": "disposable-node",
            "region_code": "US",
            "tags": [],
            "weight": 10,
            "supported_tool_types": ["claude"],
        },
    )
    assert created.status_code == 200
    node_id = created.json()["data"]["node"]["id"]

    blocked = client.delete(f"/api/v1/nodes/{node_id}", headers=auth_header(admin_token))
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "NODE_DELETE_REQUIRES_DISABLED"

    assert (
        client.post(
            f"/api/v1/nodes/{node_id}/disable", headers=auth_header(admin_token)
        ).status_code
        == 200
    )
    deleted = client.delete(f"/api/v1/nodes/{node_id}", headers=auth_header(admin_token))
    assert deleted.status_code == 200
    assert (
        client.get(f"/api/v1/nodes/{node_id}", headers=auth_header(admin_token)).status_code == 404
    )


def test_node_delete_is_blocked_by_retained_device_binding(client: TestClient) -> None:
    """
    禁用 Node 也不能级联删除受 retention 管理的设备控制历史。

    :param client (TestClient): 测试 API 客户端
    """

    admin_token = bootstrap(client)
    created = client.post(
        "/api/v1/nodes",
        headers=auth_header(admin_token),
        json={
            "name": "retained-binding-node",
            "region_code": "US",
            "tags": [],
            "weight": 10,
            "supported_tool_types": ["claude"],
        },
    )
    assert created.status_code == 200
    node_id = UUID(created.json()["data"]["node"]["id"])

    async def add_retained_binding() -> None:
        """
        添加保留绑定。
        """
        app = cast(FastAPI, client.app)
        tool_session_id = uuid4()
        async with app.state.session_factory() as session:
            session.add(
                DeviceSession(
                    user_id=uuid4(),
                    device_id=uuid4(),
                    tool_session_id=tool_session_id,
                    tool_session_reference_id=tool_session_id,
                    node_id=node_id,
                    platform="macos",
                    status="stopped",
                    generation=2,
                    expires_at=datetime.now(UTC) + timedelta(days=30),
                    stopped_at=datetime.now(UTC),
                    stop_reason="session_end",
                )
            )
            await session.commit()

    asyncio.run(add_retained_binding())
    assert (
        client.post(
            f"/api/v1/nodes/{node_id}/disable",
            headers=auth_header(admin_token),
        ).status_code
        == 200
    )
    deleted = client.delete(
        f"/api/v1/nodes/{node_id}",
        headers=auth_header(admin_token),
    )
    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "NODE_DELETE_BLOCKED"
