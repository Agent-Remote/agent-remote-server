"""
验证新管理员恢复入口的严格身份、零写读取与历史回执稳定性。
"""

import copy
import json
from typing import cast
from uuid import uuid4

import pytest
from config_import_request_support import snapshot
from fastapi.testclient import TestClient
from reviewed_runtime_recovery_support import change_rows, endpoint, inspect_url, reviewed_case
from runtime_migration_support import begin_migration
from test_config_import_requests import client as client
from test_runtime_migration_explicit_recovery import recovery_authorization
from test_tool_accounts_api import auth_header


@pytest.mark.parametrize("action", ["verify_target", "verify_source", "repair_source"])
def test_reviewed_recovery_runs_existing_node_protocol_and_preserves_history(
    client: TestClient, action: str
) -> None:
    """
    三动作均沿原租约完成，较新迁移不能使旧键变成重新执行。

    :param client (TestClient): 独立数据库客户端
    :param action (str): 明确恢复动作
    """
    case, payload = reviewed_case(client, action)
    response = client.post(endpoint(case), headers=auth_header(case.token), json=payload)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == {"schema_version", "request_id", "action", "binding", "status"}
    assert data["request_id"] == payload["request_id"] and data["action"] == action
    assert data["status"] == "pending"
    binding = data["binding"]
    assert (
        binding["version"] == {"verify_target": 1, "verify_source": 2, "repair_source": 3}[action]
    )
    assert ("action" in binding) is (action != "verify_target")
    assert (
        binding["original_task_record_id"]
        == cast(dict[str, object], payload["expected_original"])["task_record_id"]
    )
    before = snapshot(client)
    assert (
        client.post(endpoint(case), headers=auth_header(case.token), json=payload).json()["data"]
        == data
    )
    assert (
        client.get(
            endpoint(case) + "/" + str(payload["request_id"]), headers=auth_header(case.token)
        ).json()["data"]
        == data
    )
    assert snapshot(client) == before
    grant = recovery_authorization(case, binding["task_id"])
    completed = client.post(
        f"/api/v1/node-api/tasks/{binding['task_id']}/complete",
        headers=auth_header(case.node_token),
        json={"result": {"recovered": True, "authorization": grant}},
    )
    assert completed.status_code == 200, completed.text
    newer = client.post(
        f"/api/v1/tool-accounts/{case.account_id}/runtime-migration",
        headers=auth_header(case.token),
        json={
            "target_runtime_backend": "docker_sandbox" if action == "verify_target" else "native"
        },
    )
    assert newer.status_code == 200, newer.text
    change_rows(client, "UPDATE tool_accounts SET affinity_node_id=NULL")
    before = snapshot(client)
    for response in (
        client.get(
            endpoint(case) + "/" + str(payload["request_id"]), headers=auth_header(case.token)
        ),
        client.post(endpoint(case), headers=auth_header(case.token), json=payload),
    ):
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {**data, "status": "succeeded"}
    assert snapshot(client) == before


def test_authority_and_every_new_route_reject_device_without_touching_rows(
    client: TestClient,
) -> None:
    """
    管理员所属设备及伪装成用户类型的关联设备凭据都不能获取新管理权限。

    :param client (TestClient): 独立数据库客户端
    """
    case, payload = reviewed_case(client)
    registration = client.post(
        "/api/v1/devices/register",
        headers=auth_header(case.token),
        json={
            "name": "recovery-fixture",
            "platform": "linux",
            "cli_version": "test",
            "ssh_public_key": "ssh-ed25519 AAAARECOVERYFIXTURE test@example.invalid",
        },
    )
    assert registration.status_code == 200
    device = registration.json()["data"]
    token = device["device_token"]["access_token"]
    device_id = device["device"]["id"].replace("-", "")
    change_rows(
        client,
        "UPDATE user_devices SET last_seen_at='2000-01-01 00:00:00' WHERE id=?",
        (device_id,),
    )
    for token_type in ("device", "user"):
        change_rows(
            client,
            "UPDATE auth_tokens SET token_type=? WHERE user_device_id=?",
            (token_type, device_id),
        )
        before = snapshot(client)
        responses = [
            client.get("/api/v1/runtime-recovery/authority", headers=auth_header(token)),
            client.get(
                inspect_url(case),
                params={"original_task_id": case.task_id},
                headers=auth_header(token),
            ),
            client.post(endpoint(case), json=payload, headers=auth_header(token)),
            client.get(
                endpoint(case) + "/" + str(payload["request_id"]), headers=auth_header(token)
            ),
        ]
        assert all(response.status_code == 403 for response in responses)
        assert all(
            response.json()["error"]["code"] == "USER_TOKEN_REQUIRED" for response in responses
        )
        assert snapshot(client) == before


def test_authority_allows_non_admin_observation_but_not_management(client: TestClient) -> None:
    """
    当前普通用户只获得自身标志，失去管理员角色立即拒绝原键查询。

    :param client (TestClient): 独立数据库客户端
    """
    case, payload = reviewed_case(client)
    accepted = client.post(endpoint(case), json=payload, headers=auth_header(case.token))
    assert accepted.status_code == 200
    change_rows(client, "UPDATE users SET role='user'")
    before = snapshot(client)
    authority = client.get("/api/v1/runtime-recovery/authority", headers=auth_header(case.token))
    assert authority.status_code == 200
    assert authority.json()["data"] == {
        "schema_version": 1,
        "admin": False,
        "actor_user_id": cast(dict[str, object], payload["expected_original"])["user_id"],
    }
    assert (
        client.get(
            inspect_url(case),
            params={"original_task_id": case.task_id},
            headers=auth_header(case.token),
        ).status_code
        == 403
    )
    assert (
        client.post(endpoint(case), json=payload, headers=auth_header(case.token)).status_code
        == 403
    )
    assert (
        client.get(
            endpoint(case) + "/" + str(payload["request_id"]), headers=auth_header(case.token)
        ).status_code
        == 403
    )
    assert snapshot(client) == before


def test_inspection_is_safe_observation_of_active_task_not_admission(client: TestClient) -> None:
    """
    尚未终止的迁移仍能观察，返回内容严格限定且没有准入承诺。

    :param client (TestClient): 独立数据库客户端
    """
    case = begin_migration(client)
    change_rows(
        client,
        "UPDATE node_tasks SET payload=json_set(payload,'$.private','SECRET_FIXTURE') "
        "WHERE task_id=?",
        (case.task_id,),
    )
    before = snapshot(client)
    response = client.get(
        inspect_url(case),
        params={"original_task_id": case.task_id},
        headers=auth_header(case.token),
    )
    assert response.status_code == 200
    assert set(response.json()["data"]) == {
        "schema_version",
        "original",
        "original_status",
        "account_status",
    }
    assert response.json()["data"]["original_status"] == "leased"
    assert "SECRET_FIXTURE" not in response.text and "eligible" not in response.text
    assert snapshot(client) == before


@pytest.mark.parametrize(
    "field", ["task_record_id", "node_id", "user_id", "source_runtime_backend"]
)
def test_changed_review_is_rejected_before_new_task_and_same_key_conflicts(
    client: TestClient, field: str
) -> None:
    """
    未保存键身份变化与已保存键意图冲突使用不同确定错误，不写任何业务行。

    :param client (TestClient): 独立数据库客户端
    :param field (str): 改变的审核身份字段
    """
    case, payload = reviewed_case(client)
    changed = copy.deepcopy(payload)
    original = cast(dict[str, object], changed["expected_original"])
    if field == "source_runtime_backend":
        original["source_runtime_backend"], original["target_runtime_backend"] = (
            "native",
            "docker_sandbox",
        )
    else:
        original[field] = str(uuid4())
    before = snapshot(client)
    response = client.post(endpoint(case), json=changed, headers=auth_header(case.token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_MIGRATION_RECOVERY_REVIEW_CHANGED"
    assert snapshot(client) == before
    assert (
        client.post(endpoint(case), json=payload, headers=auth_header(case.token)).status_code
        == 200
    )
    before = snapshot(client)
    response = client.post(endpoint(case), json=changed, headers=auth_header(case.token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_MIGRATION_RECOVERY_CONFLICT"
    assert snapshot(client) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("schema_version", "1"),
        ("schema_version", 2),
        ("request_id", "00000000-0000-0000-0000-000000000000"),
        ("request_id", "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"),
        ("request_id", None),
        ("action", None),
        ("action", "SECRET_FIXTURE"),
        ("expected_original", None),
        ("unexpected", "SECRET_FIXTURE"),
    ],
)
def test_reviewed_strict_schema_rejects_without_echo_or_writes(
    client: TestClient, field: str, value: object
) -> None:
    """
    类型宽松值、未知动作和字段不能进入事务或出现在错误响应。

    :param client (TestClient): 独立数据库客户端
    :param field (str): 替换字段
    :param value (object): 非法测试值
    """
    case, payload = reviewed_case(client)
    payload[field] = value
    before = snapshot(client)
    response = client.post(endpoint(case), json=payload, headers=auth_header(case.token))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "COMMON_VALIDATION_ERROR"
    assert "SECRET_FIXTURE" not in response.text
    assert snapshot(client) == before


def test_reviewed_body_is_bounded_before_json_and_unknown_key_is_readonly(
    client: TestClient,
) -> None:
    """
    分块超限正文不解析，未知原键只返回未观察到且不创建任务。

    :param client (TestClient): 独立数据库客户端
    """
    case, payload = reviewed_case(client)
    before = snapshot(client)
    body = json.dumps(payload).encode() + b" " * 8192
    response = client.post(
        endpoint(case), content=iter([body[:100], body[100:]]), headers=auth_header(case.token)
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "RUNTIME_RECOVERY_REQUEST_TOO_LARGE"
    response = client.get(
        endpoint(case) + "/" + str(payload["request_id"]), headers=auth_header(case.token)
    )
    assert response.status_code == 404
    assert snapshot(client) == before


@pytest.mark.parametrize("fault", ["role", "user", "token", "expires"])
def test_saved_receipt_reauthenticates_current_user_without_writes(
    client: TestClient, fault: str
) -> None:
    """
    原回执不能绕过当前角色、用户状态和令牌撤销过期检查。

    :param client (TestClient): 独立数据库客户端
    :param fault (str): 当前认证失效条件
    """
    case, payload = reviewed_case(client)
    assert (
        client.post(endpoint(case), json=payload, headers=auth_header(case.token)).status_code
        == 200
    )
    statements = {
        "role": "UPDATE users SET role='user'",
        "user": "UPDATE users SET status='disabled'",
        "token": "UPDATE auth_tokens SET status='revoked'",
        "expires": "UPDATE auth_tokens SET expires_at='2000-01-01 00:00:00'",
    }
    change_rows(client, statements[fault])
    before = snapshot(client)
    response = client.get(
        endpoint(case) + "/" + str(payload["request_id"]), headers=auth_header(case.token)
    )
    assert response.status_code in {401, 403}
    assert snapshot(client) == before


@pytest.mark.parametrize(
    "field",
    [
        "task_id",
        "task_record_id",
        "user_id",
        "tool_account_id",
        "node_id",
        "tool_type",
        "source_runtime_backend",
        "target_runtime_backend",
    ],
)
def test_original_projection_fields_cannot_be_omitted(client: TestClient, field: str) -> None:
    """
    原投影每个字段必须显式提供，不能通过默认值放宽审核绑定。

    :param client (TestClient): 独立数据库客户端
    :param field (str): 被省略的投影字段
    """
    case, payload = reviewed_case(client)
    del cast(dict[str, object], payload["expected_original"])[field]
    before = snapshot(client)
    response = client.post(endpoint(case), headers=auth_header(case.token), json=payload)
    assert response.status_code == 422
    assert snapshot(client) == before


def test_original_record_change_after_inspection_rejects_before_acceptance(
    client: TestClient,
) -> None:
    """
    观察后原节点和数据库记录变化必须在新受理时发现，不能相信客户端旧投影。

    :param client (TestClient): 独立数据库客户端
    """
    case, payload = reviewed_case(client)
    change_rows(
        client,
        "UPDATE node_tasks SET payload=json_set(payload,'$.tool_type','codex') WHERE task_id=?",
        (case.task_id,),
    )
    before = snapshot(client)
    response = client.post(endpoint(case), json=payload, headers=auth_header(case.token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_MIGRATION_RECOVERY_REVIEW_CHANGED"
    assert snapshot(client) == before


def test_new_key_current_admission_failure_is_not_review_changed(client: TestClient) -> None:
    """
    同一原身份但迁移仍活动属于准入拒绝，不能据此自动生成另一审核或任务。

    :param client (TestClient): 独立数据库客户端
    """
    case, payload = reviewed_case(client)
    change_rows(client, "UPDATE node_tasks SET status='running' WHERE task_id=?", (case.task_id,))
    before = snapshot(client)
    response = client.post(endpoint(case), json=payload, headers=auth_header(case.token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_MIGRATION_RECOVERY_CONFLICT"
    assert snapshot(client) == before


def test_saved_key_action_conflict_and_unrecognized_status_are_safe(client: TestClient) -> None:
    """
    原键不能变更动作，未知保存状态不能回显为受信任用户文案。

    :param client (TestClient): 独立数据库客户端
    """
    case, payload = reviewed_case(client)
    response = client.post(endpoint(case), json=payload, headers=auth_header(case.token))
    assert response.status_code == 200
    binding = response.json()["data"]["binding"]
    payload["action"] = "repair_source"
    before = snapshot(client)
    rejected = client.post(endpoint(case), json=payload, headers=auth_header(case.token))
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "RUNTIME_MIGRATION_RECOVERY_CONFLICT"
    assert snapshot(client) == before
    change_rows(
        client,
        "UPDATE node_tasks SET status='SECRET_FIXTURE' WHERE task_id=?",
        (binding["task_id"],),
    )
    before = snapshot(client)
    response = client.get(
        endpoint(case) + "/" + str(payload["request_id"]), headers=auth_header(case.token)
    )
    assert response.status_code == 409
    assert "SECRET_FIXTURE" not in response.text
    assert snapshot(client) == before


def test_missing_original_and_wrong_path_have_distinct_safe_errors(client: TestClient) -> None:
    """
    原记录消失使未受理审核失效，路径不匹配则属于请求错误，均不能创建任务。

    :param client (TestClient): 独立数据库客户端
    """
    case, payload = reviewed_case(client)
    changed = copy.deepcopy(payload)
    original = cast(dict[str, object], changed["expected_original"])
    original["task_id"] = f"migrate_tool_account_runtime:{case.account_id}:{uuid4()}"
    before = snapshot(client)
    response = client.post(endpoint(case), json=changed, headers=auth_header(case.token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_MIGRATION_RECOVERY_REVIEW_CHANGED"
    wrong = endpoint(case).replace(case.account_id, str(uuid4()))
    response = client.post(wrong, json=payload, headers=auth_header(case.token))
    assert response.status_code == 422
    response = client.get(
        inspect_url(case),
        params={"original_task_id": str(original["task_id"])},
        headers=auth_header(case.token),
    )
    assert response.status_code == 404
    assert snapshot(client) == before


def test_inspection_rejects_foreign_owner_and_unknown_task_status(client: TestClient) -> None:
    """
    原任务所有者不匹配不泄露记录，未知状态不得作为安全观察返回。

    :param client (TestClient): 独立数据库客户端
    """
    case, payload = reviewed_case(client)
    change_rows(
        client, "UPDATE node_tasks SET status='SECRET_FIXTURE' WHERE task_id=?", (case.task_id,)
    )
    before = snapshot(client)
    response = client.get(
        inspect_url(case),
        params={"original_task_id": case.task_id},
        headers=auth_header(case.token),
    )
    assert response.status_code == 409 and "SECRET_FIXTURE" not in response.text
    assert snapshot(client) == before
    change_rows(
        client,
        "UPDATE node_tasks SET payload=json_set(payload,'$.user_id',?) WHERE task_id=?",
        (str(uuid4()), case.task_id),
    )
    before = snapshot(client)
    response = client.get(
        inspect_url(case),
        params={"original_task_id": case.task_id},
        headers=auth_header(case.token),
    )
    assert response.status_code == 404
    assert snapshot(client) == before
    assert payload["schema_version"] == 1
