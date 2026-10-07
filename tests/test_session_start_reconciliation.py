"""
验证通用节点对账不会在旧会话启动完成前误报中断。
"""

import asyncio
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_sessions_api import (
    auth_header,
    bootstrap,
    create_account,
    create_node,
    create_workspace,
    register_device,
)
from test_sessions_api import (
    client as client,
)

from agent_remote_server.models import NodeTask, Session


@pytest.mark.parametrize("runtime_backend", ["native", "docker_sandbox"])
@pytest.mark.parametrize("observation", ["missing", "inactive", "process_exited"])
@pytest.mark.parametrize("outcome", ["complete", "fail"])
def test_reconcile_preserves_start_until_creation_result(
    client: TestClient, runtime_backend: str, observation: str, outcome: str
) -> None:
    """
    对账跨越待领取及已领取阶段时，启动状态保持不变且真实结果仍可收敛。

    :param client (TestClient): 认证 API 测试客户端
    :param runtime_backend (str): 会话后端
    :param observation (str): 启动期间的运行时观察
    :param outcome (str): 原始创建任务的完成或失败结果
    """
    token = bootstrap(client)
    node_id, node_token = create_node(client, token, name="startup-race", weight=10)
    device_id, device_token = register_device(client, token)
    project_key = "sha256:startup-race"
    workspace_id = create_workspace(client, device_token, device_id, project_key)
    account_id = create_account(client, token)
    response = client.post(
        "/api/v1/sessions",
        headers=auth_header(token),
        json={
            "tool_type": "claude",
            "tool_account_id": account_id,
            "workspace_id": workspace_id,
            "project_key": project_key,
            "argv": [],
        },
    )
    assert response.status_code == 200
    session_id = response.json()["data"]["id"]
    assert response.json()["data"]["status"] == "starting"
    app = cast(FastAPI, client.app)

    async def set_backend() -> None:
        """
        设置待启动会话后端，不伪造运行中状态。
        """
        async with app.state.session_factory() as database:
            runtime = await database.get(Session, UUID(session_id))
            assert runtime is not None
            runtime.runtime_backend = runtime_backend
            await database.commit()

    asyncio.run(set_backend())
    reported = (
        []
        if observation == "missing"
        else [
            {
                "session_id": session_id,
                "runtime_backend": runtime_backend,
                "active": False,
                "exit_reason": observation,
            }
        ]
    )
    for phase in ("pending", "leased"):
        if phase == "leased":
            polled = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
            assert polled.status_code == 200
            assert any(
                task["task_id"] == f"create_tool_session:{session_id}"
                for task in polled.json()["data"]["tasks"]
            )
        reconciled = client.post(
            "/api/v1/node-api/reconcile",
            headers=auth_header(node_token),
            json={
                "node_id": node_id,
                "sections": ["runtime_sessions"],
                "snapshot": {"sessions": reported},
            },
        )
        assert reconciled.status_code == 200
        current = client.get(f"/api/v1/sessions/{session_id}", headers=auth_header(token))
        assert current.json()["data"]["status"] == "starting"

    async def count_cleanup_tasks() -> int:
        """
        检查启动期间没有创建可能终止原始进程的清理任务。

        :return int: 该会话的自动清理任务数量
        """
        async with app.state.session_factory() as database:
            return int(
                await database.scalar(
                    select(func.count())
                    .select_from(NodeTask)
                    .where(NodeTask.task_id == f"cleanup_tool_session:{session_id}")
                )
                or 0
            )

    assert asyncio.run(count_cleanup_tasks()) == 0
    result = client.post(
        f"/api/v1/node-api/tasks/create_tool_session:{session_id}/{outcome}",
        headers=auth_header(node_token),
        json=(
            {"result": {"session_id": session_id, "status": "running"}}
            if outcome == "complete"
            else {"error": {"code": "START_FAILED", "message": "runtime start failed"}}
        ),
    )
    assert result.status_code == 200
    current = client.get(f"/api/v1/sessions/{session_id}", headers=auth_header(token))
    assert current.json()["data"]["status"] == ("running" if outcome == "complete" else "failed")
