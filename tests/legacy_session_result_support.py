"""
通过真实会话入口建立旧协议任务并观察生命周期业务行。
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import cast

from config_import_request_support import database_path
from fastapi.testclient import TestClient
from test_sessions_api import (
    auth_header,
    bootstrap,
    create_account,
    create_node,
    create_workspace,
    register_device,
)


@dataclass(frozen=True)
class LegacySessionCase:
    """
    保存旧协议原会话和租用任务身份。
    """

    client: TestClient
    token: str = field(repr=False)
    node_token: str = field(repr=False)
    session_id: str
    task_id: str
    result: dict[str, object]
    device_id: str
    device_token: str = field(repr=False)
    node_id: str


def start_case(client: TestClient) -> LegacySessionCase:
    """
    创建真正的旧协议会话，保留尚未上报的原始启动任务。

    :param client (TestClient): 隔离文件数据库客户端
    :return LegacySessionCase: 原始启动上下文
    """
    token = bootstrap(client)
    node_id, node_token = create_node(client, token, name="session-result", weight=10)
    device, device_token = register_device(client, token)
    workspace = create_workspace(client, device_token, device, "sha256:legacy-result")
    account = create_account(client, token)
    response = client.post(
        "/api/v1/sessions",
        headers=auth_header(token),
        json={
            "tool_type": "claude",
            "tool_account_id": account,
            "workspace_id": workspace,
            "project_key": "sha256:legacy-result",
            "argv": [],
        },
    )
    assert response.status_code == 200, response.text
    session_id = response.json()["data"]["id"]
    polled = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert polled.status_code == 200
    task = next(
        task
        for task in polled.json()["data"]["tasks"]
        if task["task_type"] == "create_tool_session"
    )
    assert "skill_manager" not in task["payload"]
    return LegacySessionCase(
        client,
        token,
        node_token,
        session_id,
        task["task_id"],
        {
            "status": "running",
            "session_id": session_id,
            "tmux_session_name": task["payload"]["tmux_session_name"],
            "sandbox_name": task["payload"]["sandbox_name"],
        },
        device,
        device_token,
        node_id,
    )


def report(case: LegacySessionCase, task: str, outcome: str, value: dict[str, object]) -> int:
    """
    以原节点身份提交成功或失败回报。

    :param case (LegacySessionCase): 原始上下文
    :param task (str): 原逻辑任务
    :param outcome (str): complete 或 fail
    :param value (dict[str, object]): 合成原结果
    :return int: HTTP 状态
    """
    return case.client.post(
        f"/api/v1/node-api/tasks/{task}/{outcome}",
        headers=auth_header(case.node_token),
        json={"result" if outcome == "complete" else "error": value},
    ).status_code


def current(case: LegacySessionCase) -> dict[str, object]:
    """
    读取原会话当前展示状态。

    :param case (LegacySessionCase): 原始上下文
    :return dict[str, object]: 当前会话数据
    """
    response = case.client.get(
        f"/api/v1/sessions/{case.session_id}", headers=auth_header(case.token)
    )
    assert response.status_code == 200
    return cast(dict[str, object], response.json()["data"])


def business_rows(client: TestClient) -> str:
    """
    忽略用户互斥锁计数，只比较结果处理不得再次改变的业务记录。

    :param client (TestClient): 独立数据库客户端
    :return str: 稳定的业务数据库快照
    """
    with closing(sqlite3.connect(database_path(client))) as connection:
        return "\n".join(
            line
            for line in connection.iterdump()
            if not line.startswith('INSERT INTO "skill_storage_usage"')
        )
