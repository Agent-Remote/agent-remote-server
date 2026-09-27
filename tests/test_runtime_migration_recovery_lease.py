"""
验证完整恢复绑定的续租不会复活失效轮次或修改账户与原失败证据。
"""

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from runtime_migration_support import begin_migration, migration_state
from sqlalchemy import select
from test_runtime_migration_explicit_recovery import (
    fail_original,
    recovery_authorization,
    submit_recovery,
)
from test_tool_accounts_api import auth_header
from test_tool_accounts_api import client as client

from agent_remote_server.config import Settings
from agent_remote_server.models import NodeTask


@pytest.mark.parametrize("action", [None, "verify_source", "repair_source"])
@pytest.mark.parametrize(
    "fault", ["none", "attempt", "record", "original", "extra", "boolean", "action"]
)
def test_recovery_lease_requires_exact_current_authorization(
    client: TestClient, fault: str, action: str | None
) -> None:
    """
    续租只延长原任务，不修改账户、结果、任务绑定或领取轮次。

    :param client (TestClient): 隔离 HTTP 客户端
    :param fault (str): 原授权中的不匹配字段
    :param action (str | None): 原受理的验证动作
    """
    case = begin_migration(client)
    fail_original(case)
    accepted = submit_recovery(case, str(uuid4()), action)
    task_id = str(cast(dict[str, object], accepted["binding"])["task_id"])
    grant = recovery_authorization(case, task_id)
    payload = copy.deepcopy(grant)
    binding = cast(dict[str, object], payload["binding"])
    if fault == "action":
        if action:
            binding.pop("action")
            binding["version"] = 1
        else:
            binding["action"] = "verify_source"
            binding["version"] = 2
    elif fault == "attempt":
        payload["lease_attempt"] = 2
    elif fault == "record":
        binding["task_record_id"] = str(uuid4())
    elif fault == "original":
        binding["original_task_record_id"] = str(uuid4())
    elif fault == "extra":
        payload["ignored"] = True
    elif fault == "boolean":
        payload["lease_attempt"] = True
    before = asyncio.run(migration_state(case))

    async def task_state() -> tuple[datetime | None, int, str, dict[str, object]]:
        """
        读取当前任务的租约及不可变输入。

        :return tuple[datetime | None, int, str, dict[str, object]]: 截止时间、轮次、状态和绑定
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
            assert task is not None
            return task.lease_until, task.retry_count, task.status, task.payload

    task_before = asyncio.run(task_state())
    response = client.post(
        f"/api/v1/node-api/tasks/{task_id}/runtime-migration-recovery-lease",
        headers=auth_header(case.node_token),
        json=payload,
    )
    after = asyncio.run(task_state())
    assert asyncio.run(migration_state(case)) == before
    if fault != "none":
        assert response.status_code in {409, 422}
        assert after == task_before
        return
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["authorization"] == grant
    start = datetime.fromisoformat(data["server_time"])
    end = datetime.fromisoformat(data["lease_until"])
    assert end - start == timedelta(seconds=30)
    assert data["renew_after_milliseconds"] == 10000
    assert after[1:] == task_before[1:]
    assert after[0] is not None and task_before[0] is not None and after[0] > task_before[0]


@pytest.mark.parametrize("fault", ["expired", "terminal", "reissued", "user-token"])
def test_recovery_lease_cannot_revive_or_replace_authority(client: TestClient, fault: str) -> None:
    """
    过期、终态、重新领取和非节点身份均不能延长原授权。

    :param client (TestClient): 隔离客户端
    :param fault (str): 当前授权撤销条件
    """
    case = begin_migration(client)
    fail_original(case)
    accepted = submit_recovery(case, str(uuid4()))
    task_id = str(cast(dict[str, object], accepted["binding"])["task_id"])
    grant = recovery_authorization(case, task_id)

    async def revoke() -> datetime | None:
        """
        在独立事务中使原轮次失效并返回截止时间。

        :return datetime | None: 撤销之后的原任务截止时间
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory.begin() as session:
            task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
            assert task is not None
            if fault == "expired":
                task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            elif fault == "terminal":
                task.status = "failed"
            elif fault == "reissued":
                task.retry_count += 1
            return task.lease_until

    asyncio.run(revoke())
    response = client.post(
        f"/api/v1/node-api/tasks/{task_id}/runtime-migration-recovery-lease",
        headers=auth_header(case.token if fault == "user-token" else case.node_token),
        json=grant,
    )
    assert response.status_code in {401, 403, 404, 409}


@pytest.mark.parametrize("seconds", [0, 1, 300, 900])
def test_recovery_lease_bounds_configured_duration(client: TestClient, seconds: int) -> None:
    """
    无效时长不授权，过长配置被限制为三百秒，最短租约仍有有效续租间隔。

    :param client (TestClient): 隔离客户端
    :param seconds (int): 被验证的配置秒数
    """
    case = begin_migration(client)
    fail_original(case)
    accepted = submit_recovery(case, str(uuid4()))
    task_id = str(cast(dict[str, object], accepted["binding"])["task_id"])
    grant = recovery_authorization(case, task_id)
    app = cast(FastAPI, client.app)
    settings = cast(Settings, app.state.settings)
    settings.node_task_lease_seconds = seconds
    response = client.post(
        f"/api/v1/node-api/tasks/{task_id}/runtime-migration-recovery-lease",
        headers=auth_header(case.node_token),
        json=grant,
    )
    if seconds == 0:
        assert response.status_code == 409
        return
    assert response.status_code == 200
    data = response.json()["data"]
    duration = datetime.fromisoformat(data["lease_until"]) - datetime.fromisoformat(
        data["server_time"]
    )
    assert duration.total_seconds() == min(seconds, 300)
    assert 0 < data["renew_after_milliseconds"] < duration.total_seconds() * 1000
