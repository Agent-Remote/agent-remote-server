"""
验证后端迁移失败不重新准入账户写入，原任务结果不能篡改后续迁移。
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from runtime_migration_support import begin_migration, migration_state, reactivate_display_status
from test_skill_config_import import request as import_request
from test_tool_accounts_api import auth_header
from test_tool_accounts_api import client as client


@pytest.mark.parametrize(
    "code",
    [
        "STATE_COPY_PENDING",
        "STATE_COPY_FAILED",
        "STATE_MIGRATION_PENDING",
        "STATE_MIGRATION_FAILED",
        "RUNTIME_FAILED",
    ],
)
def test_failed_migration_preserves_recovery_gate(client: TestClient, code: str) -> None:
    """
    任意失败消息都不能证明源权限恢复，变更展示状态后仍拒绝新写入。

    :param client (TestClient): 隔离 HTTP 客户端
    :param code (str): 节点迁移失败类型
    """
    case = begin_migration(client)
    failure = {"error": {"code": code, "message": "retained migration requires inspection"}}
    for _ in range(2):
        response = client.post(
            f"/api/v1/node-api/tasks/{case.task_id}/fail",
            headers=auth_header(case.node_token),
            json=failure,
        )
        assert response.status_code == 200
    status, backend, profile = asyncio.run(migration_state(case))
    assert (status, backend) == ("migrating", "docker_sandbox")
    migration = profile["runtime_migration"]
    assert isinstance(migration, dict)
    assert migration["status"] == "recovery_required"
    assert migration["task_id"] == case.task_id
    asyncio.run(reactivate_display_status(case))
    account = f"/api/v1/tool-accounts/{case.account_id}"
    for path, payload in [
        (account + "/bind/start", {}),
        (account + "/runtime-migration", {"target_runtime_backend": "native"}),
        (account + "/config-imports", import_request(["~/.claude/settings.json"], False)),
        (
            "/api/v1/sessions",
            {
                "tool_type": "claude",
                "tool_account_id": case.account_id,
                "workspace_id": "77777777-7777-4777-8777-777777777777",
                "project_key": "recovery-test",
                "argv": [],
            },
        ),
    ]:
        denied = client.post(path, headers=auth_header(case.token), json=payload)
        assert denied.status_code == 409
        assert denied.json()["error"]["code"] == "RUNTIME_MIGRATION_PENDING"
    assert asyncio.run(migration_state(case))[2] == profile


def test_pending_migration_cannot_be_replaced(client: TestClient) -> None:
    """
    原任务仍在执行时不能以新的逻辑任务覆盖迁移档案。

    :param client (TestClient): 隔离 HTTP 客户端
    """
    case = begin_migration(client)
    before = asyncio.run(migration_state(case))
    response = client.post(
        f"/api/v1/tool-accounts/{case.account_id}/runtime-migration",
        headers=auth_header(case.token),
        json={"target_runtime_backend": "native"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_MIGRATION_PENDING"
    assert asyncio.run(migration_state(case)) == before


def test_original_success_replay_cannot_change_later_migration(client: TestClient) -> None:
    """
    新迁移受理后重放旧成功仍只读，旧失败不能覆盖已经确认的成功。

    :param client (TestClient): 隔离 HTTP 客户端
    """
    case = begin_migration(client)
    success = {"result": {"migrated": True, "runtime_backend": "native"}}
    endpoint = f"/api/v1/node-api/tasks/{case.task_id}"
    assert (
        client.post(
            endpoint + "/complete", headers=auth_header(case.node_token), json=success
        ).status_code
        == 200
    )
    response = client.post(
        f"/api/v1/tool-accounts/{case.account_id}/runtime-migration",
        headers=auth_header(case.token),
        json={"target_runtime_backend": "docker_sandbox"},
    )
    assert response.status_code == 200
    before = asyncio.run(migration_state(case))
    assert (
        client.post(
            endpoint + "/complete", headers=auth_header(case.node_token), json=success
        ).status_code
        == 200
    )
    assert asyncio.run(migration_state(case)) == before
    conflict = client.post(
        endpoint + "/fail",
        headers=auth_header(case.node_token),
        json={"error": {"code": "STATE_MIGRATION_PENDING"}},
    )
    assert conflict.status_code == 409
    assert asyncio.run(migration_state(case)) == before


@pytest.mark.parametrize(
    "result", [{"migrated": False}, {"migrated": True, "runtime_backend": "docker_sandbox"}]
)
def test_malformed_success_does_not_reopen_source(
    client: TestClient, result: dict[str, object]
) -> None:
    """
    缺少目标成功证据时不接受终态，也不恢复源账户准入。

    :param client (TestClient): 隔离 HTTP 客户端
    :param result (dict[str, object]): 与目标不匹配的成功内容
    """
    case = begin_migration(client)
    before = asyncio.run(migration_state(case))
    response = client.post(
        f"/api/v1/node-api/tasks/{case.task_id}/complete",
        headers=auth_header(case.node_token),
        json={"result": result},
    )
    assert response.status_code == 409
    assert asyncio.run(migration_state(case)) == before


@pytest.mark.parametrize("outcome", ["complete", "fail"])
def test_migration_result_preserves_intervening_disable(client: TestClient, outcome: str) -> None:
    """
    用户在迁移期间禁用账户后，迟到的成功或失败不能重新激活它。

    :param client (TestClient): 隔离 HTTP 客户端
    :param outcome (str): 成功或失败结果入口
    """
    case = begin_migration(client)
    response = client.post(
        f"/api/v1/tool-accounts/{case.account_id}/disable", headers=auth_header(case.token)
    )
    assert response.status_code == 200
    payload = (
        {"result": {"migrated": True, "runtime_backend": "native"}}
        if outcome == "complete"
        else {"error": {"code": "STATE_MIGRATION_PENDING"}}
    )
    response = client.post(
        f"/api/v1/node-api/tasks/{case.task_id}/{outcome}",
        headers=auth_header(case.node_token),
        json=payload,
    )
    assert response.status_code == 200
    status, backend, _ = asyncio.run(migration_state(case))
    assert status == "disabled"
    assert backend == ("native" if outcome == "complete" else "docker_sandbox")
