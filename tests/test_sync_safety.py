"""
验证同步准备结果、冲突确认和精确密钥授权的边界。
"""

import asyncio
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_workspaces_api import (
    auth_header,
    bootstrap,
    create_node,
    create_workspace,
    register_device,
)
from test_workspaces_api import client as client

from agent_remote_server.models import NodeTask, SshKey, SyncSession


@dataclass
class SyncFixture:
    """
    保存隔离同步测试目标和凭据。
    """

    node: str
    node_token: str
    device: str
    device_token: str
    sync: str
    task: str
    key: str


def prepare_sync(client: TestClient) -> SyncFixture:
    """
    创建隔离准备任务而不执行任何真实节点操作。

    :param client (TestClient): 隔离测试客户端
    :return SyncFixture: 原同步目标
    """
    token = bootstrap(client)
    node, node_token = create_node(client, token)
    device, device_token = register_device(client, token)
    workspace = create_workspace(client, device_id=device, device_token=device_token)
    response = client.post(
        "/api/v1/sync-sessions",
        headers=auth_header(device_token),
        json={"workspace_id": workspace["id"], "node_id": node},
    )
    assert response.status_code == 200
    sync = response.json()["data"]
    tasks = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert tasks.status_code == 200

    async def key_id() -> str:
        """
        读取隔离设备的公开密钥标识。

        :return str: 密钥标识
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            key = await session.scalar(select(SshKey).where(SshKey.user_device_id == UUID(device)))
            assert key is not None
            return str(key.id)

    return SyncFixture(
        node,
        node_token,
        device,
        device_token,
        sync["id"],
        tasks.json()["data"]["tasks"][0]["task_id"],
        asyncio.run(key_id()),
    )


@pytest.mark.parametrize("outcome", ["complete", "fail"])
@pytest.mark.parametrize(
    "drift",
    [
        "valid",
        "paused",
        "stopped",
        "active",
        "node",
        "workspace",
        "user",
        "device",
        "remote_path",
        "task_id",
    ],
)
def test_prepare_result_preserves_current_target(
    client: TestClient, outcome: str, drift: str
) -> None:
    """
    验证迟到结果不能覆盖当前暂停或其他目标。

    :param client (TestClient): 隔离客户端
    :param outcome (str): 任务结果类型
    :param drift (str): 变化的状态或目标字段
    """
    fixture = prepare_sync(client)

    async def change() -> str:
        """
        模拟准备过程中发生目标或生命周期变化。

        :return str: 保留的同步状态
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            sync = await session.get(SyncSession, UUID(fixture.sync))
            task = await session.scalar(select(NodeTask).where(NodeTask.task_id == fixture.task))
            assert sync is not None and task is not None
            if drift == "valid":
                pass
            elif drift in {"paused", "stopped", "active"}:
                sync.status = drift
            elif drift == "task_id":
                task.task_id = "foreign-preparation"
                fixture.task = task.task_id
            elif drift == "node":
                sync.node_id = None
            else:
                field = {"workspace": "workspace_id", "user": "user_id", "device": "device_id"}.get(
                    drift, drift
                )
                task.payload = {**task.payload, field: str(uuid4())}
            await session.commit()
            return str(sync.status)

    expected = asyncio.run(change())
    if drift == "valid":
        expected = "active" if outcome == "complete" else "failed"
    body = (
        {"result": {"status": "prepared"}}
        if outcome == "complete"
        else {"error": {"code": "TEST_FAILURE"}}
    )
    response = client.post(
        f"/api/v1/node-api/tasks/{fixture.task}/{outcome}",
        headers=auth_header(fixture.node_token),
        json=body,
    )
    assert response.status_code == 200
    current = client.get(
        f"/api/v1/sync-sessions/{fixture.sync}", headers=auth_header(fixture.device_token)
    )
    assert current.json()["data"]["status"] == expected


@pytest.mark.parametrize(
    "before,after",
    [
        ("active", "active"),
        ("paused", "paused"),
        ("starting", "starting"),
        ("failed", "failed"),
        ("healthy", "active"),
    ],
)
def test_resolve_retains_lifecycle(client: TestClient, before: str, after: str) -> None:
    """
    验证冲突确认不重启同步且可显式修复旧健康状态。

    :param client (TestClient): 隔离客户端
    :param before (str): 原生命周期
    :param after (str): 预期生命周期
    """
    fixture = prepare_sync(client)

    async def change() -> None:
        """
        准备冲突元数据。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            sync = await session.get(SyncSession, UUID(fixture.sync))
            assert sync is not None
            sync.status, sync.conflict_status = before, "has_conflicts"
            await session.commit()

    asyncio.run(change())
    response = client.post(
        f"/api/v1/sync-sessions/{fixture.sync}/resolve",
        headers=auth_header(fixture.device_token),
        json={},
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == after
    assert response.json()["data"]["conflict_status"] == "none"
    if after == "active":
        verified = client.post(
            "/api/v1/node-api/sync/verify",
            headers=auth_header(fixture.node_token),
            json={"node_id": fixture.node, "device_id": fixture.device},
        )
        assert verified.status_code == 200


@pytest.mark.parametrize("key_state", ["active", "revoked", "foreign"])
def test_sync_gateway_checks_exact_key(client: TestClient, key_state: str) -> None:
    """
    验证实际密钥被撤销或不属于设备时拒绝同步。

    :param client (TestClient): 隔离客户端
    :param key_state (str): 密钥状态
    """
    fixture = prepare_sync(client)

    async def revoke() -> None:
        """
        撤销仍可能残留在节点文件内的密钥。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            key = await session.get(SshKey, UUID(fixture.key))
            assert key is not None
            key.status = "revoked"
            await session.commit()

    if key_state == "revoked":
        asyncio.run(revoke())
    key = str(uuid4()) if key_state == "foreign" else fixture.key
    response = client.post(
        "/api/v1/node-api/sync/verify",
        headers=auth_header(fixture.node_token),
        json={"node_id": fixture.node, "device_id": fixture.device, "ssh_key_id": key},
    )
    if key_state == "active":
        assert response.status_code == 200
        assert response.json()["data"]["ssh_key_id"] == key
    else:
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "SSH_KEY_REVOKED"


@pytest.mark.parametrize("outcome", ["active", "failed"])
def test_prepare_publication_ignores_stale_loaded_state(client: TestClient, outcome: str) -> None:
    """
    验证同一事务缓存旧状态时仍以数据库当前暂停状态为准。

    :param client (TestClient): 隔离客户端
    :param outcome (str): 准备结果
    """
    fixture = prepare_sync(client)

    async def publish_after_pause() -> None:
        """
        先加载旧对象，再以独立 SQL 模拟暂停后发布结果。
        """
        from sqlalchemy import update

        from agent_remote_server.services.sync_task_results import publish_sync_preparation

        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            sync = await session.get(SyncSession, UUID(fixture.sync))
            task = await session.scalar(select(NodeTask).where(NodeTask.task_id == fixture.task))
            assert sync is not None and task is not None
            await session.execute(
                update(SyncSession)
                .where(SyncSession.id == sync.id)
                .values(status="paused")
                .execution_options(synchronize_session=False)
            )
            assert sync.status == "starting"
            await publish_sync_preparation(
                session, task, "active" if outcome == "active" else "failed"
            )
            await session.refresh(sync)
            assert sync.status == "paused"

    asyncio.run(publish_after_pause())


@pytest.mark.parametrize("action", ["pause", "resume", "resolve", "reset"])
@pytest.mark.parametrize(
    "field",
    [
        "id",
        "workspace_id",
        "node_id",
        "local_path",
        "remote_path",
        "mutagen_session_id",
        "remote_endpoint",
        "sync_mode",
        "sync_git",
        "exclude",
        "valid",
    ],
)
def test_sync_actions_compare_confirmed_target(client: TestClient, action: str, field: str) -> None:
    """
    验证每个同步动作在修改前比较完整确认目标。

    :param client (TestClient): 隔离客户端
    :param action (str): 同步动作
    :param field (str): 与确认不同的字段
    """
    fixture = prepare_sync(client)
    path = f"/api/v1/sync-sessions/{fixture.sync}"
    before = client.get(path, headers=auth_header(fixture.device_token)).json()["data"]
    assert before["target_precondition_version"] == 1
    assert before["target_precondition_checked"] is False
    expected = {
        name: before[name]
        for name in (
            "id",
            "workspace_id",
            "node_id",
            "local_path",
            "remote_path",
            "mutagen_session_id",
            "remote_endpoint",
            "sync_mode",
            "sync_git",
            "exclude",
        )
    }
    if field == "sync_git":
        expected[field] = not expected[field]
    elif field == "exclude":
        expected[field] = ["different-rule"]
    elif field in {"id", "workspace_id", "node_id"}:
        expected[field] = str(uuid4())
    elif field != "valid":
        expected[field] = "different-target"
    response = client.post(
        f"{path}/{action}",
        headers=auth_header(fixture.device_token),
        json={"expected_target": expected},
    )
    if field == "valid":
        assert response.status_code == 200
        assert response.json()["data"]["target_precondition_version"] == 1
        assert response.json()["data"]["target_precondition_checked"] is True
    else:
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SYNC_TARGET_CHANGED"
        after = client.get(path, headers=auth_header(fixture.device_token)).json()["data"]
        assert after == before


@pytest.mark.parametrize(
    "status,allowed",
    [
        ("conflicted", True),
        ("healthy", True),
        ("paused", True),
        ("failed", False),
        ("stopped", False),
    ],
)
def test_sync_transport_can_repair_conflicts(
    client: TestClient, status: str, allowed: bool
) -> None:
    """
    验证冲突状态仍允许重新扫描，失败或停止状态保持拒绝。

    :param client (TestClient): 隔离客户端
    :param status (str): 同步生命周期
    :param allowed (bool): 是否允许受限同步连接
    """
    fixture = prepare_sync(client)

    async def change() -> None:
        """
        设置待修复状态，不执行真实文件同步。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            sync = await session.get(SyncSession, UUID(fixture.sync))
            assert sync is not None
            sync.status, sync.conflict_status = status, "has_conflicts"
            await session.commit()

    asyncio.run(change())
    for resolved in [False, True]:
        if resolved:
            response = client.post(
                f"/api/v1/sync-sessions/{fixture.sync}/resolve",
                headers=auth_header(fixture.device_token),
                json={},
            )
            assert response.status_code == 200
        response = client.post(
            "/api/v1/node-api/sync/verify",
            headers=auth_header(fixture.node_token),
            json={"node_id": fixture.node, "device_id": fixture.device, "ssh_key_id": fixture.key},
        )
        assert response.status_code == (200 if allowed else 403)
