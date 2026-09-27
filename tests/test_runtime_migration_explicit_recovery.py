"""
验证管理员显式检查原迁移的独立任务、租约及不可变终态。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from runtime_migration_support import RuntimeMigrationCase, begin_migration, migration_state
from sqlalchemy import select
from test_tool_accounts_api import auth_header
from test_tool_accounts_api import client as client

from agent_remote_server.models import (
    NodeTask,
    NodeTaskResult,
    ToolAccount,
    ToolAccountProfile,
    User,
)
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.services.runtime_recovery import RECOVERY_FAILURE, RECOVERY_REPAIR_FAILURE


def submit_recovery(
    case: RuntimeMigrationCase, key: str, action: str | None = None
) -> dict[str, object]:
    """
    保留失败结果后，由管理员受理精确原任务的新恢复请求。

    :param case (RuntimeMigrationCase): 原迁移上下文
    :param key (str): 独立幂等键
    :param action (str | None): 可选源恢复验证动作
    :return dict[str, object]: 恢复响应
    """
    response = case.client.post(
        f"/api/v1/tool-accounts/{case.account_id}/runtime-migration/recover",
        headers=auth_header(case.token),
        json={
            "original_task_id": case.task_id,
            "request_id": key,
            **({"action": action} if action else {}),
        },
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, object], response.json()["data"])


def fail_original(case: RuntimeMigrationCase) -> None:
    """
    模拟原节点在完整本地完成证明到达前上报失败。

    :param case (RuntimeMigrationCase): 原迁移上下文
    """
    response = case.client.post(
        f"/api/v1/node-api/tasks/{case.task_id}/fail",
        headers=auth_header(case.node_token),
        json={"error": {"code": "STATE_MIGRATION_PENDING"}},
    )
    assert response.status_code == 200


def recovery_authorization(case: RuntimeMigrationCase, task_id: str) -> dict[str, object]:
    """
    真实轮询恢复任务后读取即时授权。

    :param case (RuntimeMigrationCase): 原迁移上下文
    :param task_id (str): 恢复逻辑任务
    :return dict[str, object]: 精确授权
    """
    response = case.client.post("/api/v1/node-api/tasks/poll", headers=auth_header(case.node_token))
    assert response.status_code == 200
    response = case.client.get(
        f"/api/v1/node-api/tasks/{task_id}/runtime-migration-recovery-authorization",
        headers=auth_header(case.node_token),
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, object], response.json()["data"])


@pytest.mark.parametrize("action", [None, "verify_source", "repair_source"])
def test_explicit_recovery_preserves_original_failure_and_disabled_account(
    client: TestClient,
    action: str | None,
) -> None:
    """
    成功仅结算原档案，原失败及中途禁用保持不变，重放不能改写后续迁移。

    :param client (TestClient): 隔离客户端
    :param action (str | None): 验证目标完成或源恢复
    """
    case = begin_migration(client)
    fail_original(case)
    key = str(uuid4())
    accepted = submit_recovery(case, key, action)
    assert submit_recovery(case, key, action) == accepted
    task = str(cast(dict[str, object], accepted["binding"])["task_id"])
    authorization = recovery_authorization(case, task)

    async def disable() -> None:
        """
        在恢复检查期间禁用账户。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory.begin() as session:
            account = await session.get(ToolAccount, UUID(case.account_id))
            assert account is not None
            account.status = "disabled"

    asyncio.run(disable())
    result = {"result": {"recovered": True, "authorization": authorization}}
    for _ in range(2):
        response = client.post(
            f"/api/v1/node-api/tasks/{task}/complete",
            headers=auth_header(case.node_token),
            json=result,
        )
        assert response.status_code == 200, response.text
    status, backend, profile = asyncio.run(migration_state(case))
    assert (status, backend) == ("disabled", "docker_sandbox" if action else "native")
    assert cast(dict[str, object], profile["runtime_migration"])["status"] == (
        "rolled_back" if action else "succeeded"
    )

    async def original_unchanged() -> None:
        """
        直接读取原任务及原结果验证不可变失败。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            original = await session.scalar(
                select(NodeTask).where(NodeTask.task_id == case.task_id)
            )
            saved = await session.scalar(
                select(NodeTaskResult).where(NodeTaskResult.task_id == case.task_id)
            )
            assert original is not None and saved is not None
            assert original.status == saved.status == "failed"
            assert saved.error == {"code": "STATE_MIGRATION_PENDING"}

    asyncio.run(original_unchanged())
    response = client.post(
        f"/api/v1/tool-accounts/{case.account_id}/runtime-migration",
        headers=auth_header(case.token),
        json={"target_runtime_backend": "native" if action else "docker_sandbox"},
    )
    assert response.status_code == 200
    newer = asyncio.run(migration_state(case))
    response = client.post(
        f"/api/v1/node-api/tasks/{task}/complete", headers=auth_header(case.node_token), json=result
    )
    assert response.status_code == 200
    assert asyncio.run(migration_state(case)) == newer
    assert submit_recovery(case, key, action)["status"] == "succeeded"


@pytest.mark.parametrize(
    "fault",
    [
        "pending-original",
        "active-recovery",
        "wrong-original",
        "unleased",
        "wrong-attempt",
        "bad-result",
        "conflicting-key",
    ],
)
def test_explicit_recovery_rejects_missing_current_authority(
    client: TestClient, fault: str
) -> None:
    """
    拒绝并行恢复、外来身份、未租用或错误结果，不修改账户后端。

    :param client (TestClient): 隔离客户端
    :param fault (str): 不满足的授权前提
    """
    case = begin_migration(client)
    key = str(uuid4())
    base = f"/api/v1/tool-accounts/{case.account_id}/runtime-migration/recover"
    request = {"original_task_id": case.task_id, "request_id": key}
    if fault != "pending-original":
        fail_original(case)
    if fault == "pending-original":
        response = client.post(base, headers=auth_header(case.token), json=request)
    else:
        accepted = submit_recovery(case, key)
        task = str(cast(dict[str, object], accepted["binding"])["task_id"])
        if fault in {"active-recovery", "wrong-original", "conflicting-key"}:
            if fault == "active-recovery":
                request["request_id"] = str(uuid4())
            else:
                request["original_task_id"] = case.task_id[:-36] + str(uuid4())
                if fault == "wrong-original":
                    request["request_id"] = str(uuid4())
            response = client.post(base, headers=auth_header(case.token), json=request)
        elif fault == "unleased":
            response = client.get(
                f"/api/v1/node-api/tasks/{task}/runtime-migration-recovery-authorization",
                headers=auth_header(case.node_token),
            )
        else:
            grant = recovery_authorization(case, task)
            result: dict[str, object] = {"recovered": True, "authorization": grant}
            if fault == "wrong-attempt":
                grant["lease_attempt"] = 999
            else:
                result["backup_path"] = "/untrusted/private/path"
            response = client.post(
                f"/api/v1/node-api/tasks/{task}/complete",
                headers=auth_header(case.node_token),
                json={"result": result},
            )
    assert response.status_code in {404, 409}
    assert asyncio.run(migration_state(case))[1] == "docker_sandbox"


@pytest.mark.parametrize(
    "fault",
    [
        "expired",
        "owner-disabled",
        "owner-changed",
        "node-changed",
        "backend-changed",
        "profile-changed",
        "migrating",
        "original-active",
        "original-record-changed",
    ],
)
def test_recovery_rechecks_authority_before_inspection_and_result(
    client: TestClient, fault: str
) -> None:
    """
    受理后任何原归属或租约改变，读取与结果受理都不能沿用旧授权。

    :param client (TestClient): 独立客户端
    :param fault (str): 改变的当前授权条件
    """
    case = begin_migration(client)
    fail_original(case)
    accepted = submit_recovery(case, str(uuid4()))
    task_id = str(cast(dict[str, object], accepted["binding"])["task_id"])
    grant = recovery_authorization(case, task_id)

    async def revoke() -> None:
        """
        在新事务中改变当前权威，不操作原节点证据。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory.begin() as session:
            task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
            original = await session.scalar(
                select(NodeTask).where(NodeTask.task_id == case.task_id)
            )
            account = await session.get(ToolAccount, UUID(case.account_id))
            assert task is not None and original is not None and account is not None
            if fault == "expired":
                task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            elif fault == "owner-disabled":
                owner = await session.get(User, account.user_id)
                assert owner is not None
                owner.status = "disabled"
            elif fault == "owner-changed":
                original.payload = {**original.payload, "user_id": str(uuid4())}
            elif fault == "node-changed":
                account.affinity_node_id = None
            elif fault == "backend-changed":
                account.runtime_backend = "native"
            elif fault == "profile-changed":
                profile = await session.scalar(
                    select(ToolAccountProfile).where(
                        ToolAccountProfile.tool_account_id == account.id
                    )
                )
                assert profile is not None
                profile.profile_json = {
                    **profile.profile_json,
                    "runtime_migration": {"task_id": "newer", "status": "pending"},
                }
            elif fault == "migrating":
                session.add(
                    AccountSkillDirectoryState(
                        user_id=account.user_id,
                        account_id=account.id,
                        tool_type=account.tool_type,
                        mode="migrating",
                        epoch=1,
                    )
                )
            elif fault == "original-active":
                original.status = "running"
            else:
                task.payload = {**task.payload, "original_task_record_id": str(uuid4())}

    asyncio.run(revoke())
    before = asyncio.run(migration_state(case))
    node = auth_header(case.node_token)
    denied = client.get(
        f"/api/v1/node-api/tasks/{task_id}/runtime-migration-recovery-authorization", headers=node
    )
    assert denied.status_code in {404, 409}
    denied = client.post(
        f"/api/v1/node-api/tasks/{task_id}/runtime-migration-recovery-lease",
        headers=node,
        json=grant,
    )
    assert denied.status_code in {404, 409}
    denied = client.post(
        f"/api/v1/node-api/tasks/{task_id}/complete",
        headers=node,
        json={"result": {"recovered": True, "authorization": grant}},
    )
    assert denied.status_code in {404, 409}
    assert asyncio.run(migration_state(case)) == before


@pytest.mark.parametrize("action", [None, "verify_source", "repair_source"])
def test_failed_recovery_retains_gate_and_allows_distinct_check(
    client: TestClient, action: str | None
) -> None:
    """
    失败检查保持原失败和阻塞状态，新键只能在原检查终态之后受理。

    :param client (TestClient): 隔离客户端
    :param action (str | None): 所选验证动作
    """
    case = begin_migration(client)
    fail_original(case)
    key = str(uuid4())
    accepted = submit_recovery(case, key, action)
    task_id = str(cast(dict[str, object], accepted["binding"])["task_id"])
    grant = recovery_authorization(case, task_id)
    failure = {
        "error": {
            **(RECOVERY_REPAIR_FAILURE if action == "repair_source" else RECOVERY_FAILURE),
            "lease_attempt": grant["lease_attempt"],
        }
    }
    for _ in range(2):
        response = client.post(
            f"/api/v1/node-api/tasks/{task_id}/fail",
            headers=auth_header(case.node_token),
            json=failure,
        )
        assert response.status_code == 200
    before = asyncio.run(migration_state(case))
    assert before[1] == "docker_sandbox"
    assert cast(dict[str, object], before[2]["runtime_migration"])["status"] == "recovery_required"
    response = client.post(
        f"/api/v1/node-api/tasks/{task_id}/complete",
        headers=auth_header(case.node_token),
        json={"result": {"recovered": True, "authorization": grant}},
    )
    assert response.status_code == 409
    assert asyncio.run(migration_state(case)) == before
    assert submit_recovery(case, key, action)["status"] == "failed"
    assert submit_recovery(case, str(uuid4()), action)["status"] == "pending"
    response = client.get(
        f"/api/v1/tool-accounts/{case.account_id}/runtime-migration/recover/{key}",
        headers=auth_header(case.token),
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "failed"


def test_runtime_recovery_remains_administrator_only(client: TestClient) -> None:
    """
    普通用户或节点不能借恢复入口获得管理员迁移权威。

    :param client (TestClient): 隔离客户端
    """
    case = begin_migration(client)
    fail_original(case)
    key = str(uuid4())
    submit_recovery(case, key)

    async def remove_admin_role() -> None:
        """
        保持原账户归属活动，但取消调用用户的管理员角色。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory.begin() as session:
            account = await session.get(ToolAccount, UUID(case.account_id))
            assert account is not None
            owner = await session.get(User, account.user_id)
            assert owner is not None
            owner.role = "user"

    asyncio.run(remove_admin_role())
    base = f"/api/v1/tool-accounts/{case.account_id}/runtime-migration/recover"
    before = asyncio.run(migration_state(case))
    for token in [case.token, case.node_token]:
        response = client.post(
            base,
            headers=auth_header(token),
            json={"original_task_id": case.task_id, "request_id": key},
        )
        assert response.status_code in {401, 403}
        response = client.get(base + "/" + key, headers=auth_header(token))
        assert response.status_code in {401, 403}
    assert asyncio.run(migration_state(case)) == before


def test_recovery_status_and_exact_replay_do_not_mutate_storage_rows(client: TestClient) -> None:
    """
    状态读取与同键重放连用户计量锁版本也不能修改。

    :param client (TestClient): 独立客户端
    """
    case = begin_migration(client)
    fail_original(case)
    key = str(uuid4())
    accepted = submit_recovery(case, key)

    async def lock_version() -> int:
        """
        读取既有计量锁行版本。

        :return int: 原用户计量锁版本
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            account = await session.get(ToolAccount, UUID(case.account_id))
            assert account is not None
            usage = await session.get(SkillStorageUsage, account.user_id)
            assert usage is not None
            return usage.lock_version

    before = asyncio.run(lock_version())
    assert submit_recovery(case, key) == accepted
    response = client.get(
        f"/api/v1/tool-accounts/{case.account_id}/runtime-migration/recover/{key}",
        headers=auth_header(case.token),
    )
    assert response.status_code == 200
    assert asyncio.run(lock_version()) == before

    task_id = str(cast(dict[str, object], accepted["binding"])["task_id"])
    grant = recovery_authorization(case, task_id)
    result = {"result": {"recovered": True, "authorization": grant}}
    response = client.post(
        f"/api/v1/node-api/tasks/{task_id}/complete",
        headers=auth_header(case.node_token),
        json=result,
    )
    assert response.status_code == 200
    before = asyncio.run(lock_version())
    response = client.post(
        f"/api/v1/node-api/tasks/{task_id}/complete",
        headers=auth_header(case.node_token),
        json=result,
    )
    assert response.status_code == 200
    assert submit_recovery(case, key)["status"] == "succeeded"
    assert asyncio.run(lock_version()) == before


@pytest.mark.parametrize("action", [None, "verify_source", "repair_source"])
def test_recovery_action_cannot_change_under_original_key_or_result(
    client: TestClient, action: str | None
) -> None:
    """
    相同请求键及原租约结果均不能替换验证方向。

    :param client (TestClient): 隔离客户端
    :param action (str | None): 原受理动作
    """
    case = begin_migration(client)
    fail_original(case)
    key = str(uuid4())
    accepted = submit_recovery(case, key, action)
    binding = cast(dict[str, object], accepted["binding"])
    assert len(binding) == (12 if action else 11)
    other_action = "verify_source" if action == "repair_source" else "repair_source"
    other = {"action": other_action}
    changed = client.post(
        f"/api/v1/tool-accounts/{case.account_id}/runtime-migration/recover",
        headers=auth_header(case.token),
        json={"original_task_id": case.task_id, "request_id": key, **other},
    )
    assert changed.status_code == 409
    task = str(binding["task_id"])
    grant = recovery_authorization(case, task)
    substituted = cast(dict[str, object], grant["binding"])
    substituted["action"] = other_action
    substituted["version"] = 2 if other_action == "verify_source" else 3
    before = asyncio.run(migration_state(case))
    response = client.post(
        f"/api/v1/node-api/tasks/{task}/complete",
        headers=auth_header(case.node_token),
        json={"result": {"recovered": True, "authorization": grant}},
    )
    assert response.status_code == 409
    assert asyncio.run(migration_state(case)) == before
