"""
通过真实账户和节点 HTTP 入口建立后端迁移测试，不伪造迁移任务或结果。
"""

import asyncio
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_tool_accounts_api import (
    auth_header,
    bootstrap,
    create_and_register_node,
    create_tool_account,
)

from agent_remote_server.models import Node, ToolAccount, ToolAccountProfile


@dataclass
class RuntimeMigrationCase:
    """
    保存隔离测试身份，认证字段不进入对象显示。
    """

    client: TestClient
    token: str = field(repr=False)
    node_token: str = field(repr=False)
    account_id: str
    task_id: str


def begin_migration(client: TestClient) -> RuntimeMigrationCase:
    """
    配置已有账户所在节点后，由管理员受理并实际轮询原始迁移任务。

    :param client (TestClient): 独立数据库的测试客户端
    :return RuntimeMigrationCase: 已租用任务及所属账户
    """
    token = bootstrap(client)
    node_id, node_token = create_and_register_node(client, token)
    account_id = str(create_tool_account(client, token)["id"])

    async def ready() -> None:
        """
        建立迁移前已经登录的账户和双后端节点条件。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory.begin() as session:
            node = await session.get(Node, UUID(node_id))
            account = await session.get(ToolAccount, UUID(account_id))
            assert node is not None and account is not None
            node.allowed_runtime_backends = ["docker_sandbox", "native"]
            node.runtime_capabilities = {"backends": ["docker_sandbox", "native"]}
            account.status = "active"
            account.runtime_backend = "docker_sandbox"
            account.affinity_node_id = node.id

    asyncio.run(ready())
    response = client.post(
        f"/api/v1/tool-accounts/{account_id}/runtime-migration",
        headers=auth_header(token),
        json={"target_runtime_backend": "native"},
    )
    assert response.status_code == 200
    task_id = response.json()["data"]["task_id"]
    polled = client.post("/api/v1/node-api/tasks/poll", headers=auth_header(node_token))
    assert polled.status_code == 200
    assert any(task["task_id"] == task_id for task in polled.json()["data"]["tasks"])
    return RuntimeMigrationCase(client, token, node_token, account_id, task_id)


async def migration_state(case: RuntimeMigrationCase) -> tuple[str, str | None, dict[str, object]]:
    """
    通过新事务读取账户和迁移档案，避免复用 ORM 缓存作为结果证据。

    :param case (RuntimeMigrationCase): 隔离迁移上下文
    :return tuple[str, str | None, dict[str, object]]: 状态、后端和原始档案
    """
    app = cast(FastAPI, case.client.app)
    async with app.state.session_factory() as session:
        account = await session.get(ToolAccount, UUID(case.account_id))
        profile = await session.scalar(
            select(ToolAccountProfile).where(
                ToolAccountProfile.tool_account_id == UUID(case.account_id)
            )
        )
        assert account is not None and profile is not None
        return account.status, account.runtime_backend, profile.profile_json


async def reactivate_display_status(case: RuntimeMigrationCase) -> None:
    """
    模拟迟到验证或状态编辑，不能用展示状态替代迁移权威。

    :param case (RuntimeMigrationCase): 隔离迁移上下文
    """
    app = cast(FastAPI, case.client.app)
    async with app.state.session_factory.begin() as session:
        account = await session.get(ToolAccount, UUID(case.account_id))
        assert account is not None
        account.status = "active"
