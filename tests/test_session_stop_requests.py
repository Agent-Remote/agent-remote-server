"""
验证工具会话停止原键、准确任务身份和删除后的只读观察。
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from legacy_session_result_support import current, report, start_case
from test_config_import_requests import client as client_fixture  # noqa: F401
from test_sessions_api import auth_header


@pytest.fixture
def client(client_fixture: TestClient) -> TestClient:  # noqa: F811
    """
    以本文件名称暴露共享的隔离客户端 fixture。

    :param client_fixture (TestClient): 共享客户端
    :return TestClient: 当前用例客户端
    """
    return client_fixture


def stop_payload(request_id: str) -> dict[str, object]:
    """
    构造不含命令正文的停止调用方请求。

    :param request_id (str): 原始调用方键
    :return dict[str, object]: API 请求正文
    """
    return {"request_id": request_id, "schema_version": 1, "reason": "user_requested"}


def test_stop_request_replays_exact_task_and_observes_lifecycle(client: TestClient) -> None:
    """
    同键重放返回同一停止任务，回报后只更新当前观察。

    :param client (TestClient): 隔离客户端
    """
    case = start_case(client)
    assert report(case, case.task_id, "complete", case.result) == 200
    request_id = str(uuid4())
    path = f"/api/v1/sessions/{case.session_id}/stop"
    request = stop_payload(request_id)
    request.update(
        {
            "expected_session_id": case.session_id,
            "expected_node_id": case.node_id,
            "expected_runtime_backend": current(case)["runtime_backend"],
        }
    )
    first = client.post(path, headers=auth_header(case.token), json=request)
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["session_id"] == case.session_id
    assert data["stop_task_id"] == f"stop_tool_session:{case.session_id}"
    assert data["stop_task_record_id"]
    assert data["session_status"] == "stopping"
    second = client.post(path, headers=auth_header(case.token), json=request)
    assert second.status_code == 200, second.text
    assert second.json()["data"]["stop_task_record_id"] == data["stop_task_record_id"]
    observed = client.get(
        f"/api/v1/sessions/stop-requests/{request_id}", headers=auth_header(case.token)
    )
    assert observed.status_code == 200
    assert observed.json()["data"]["stop_task_id"] == data["stop_task_id"]
    node_task = data["stop_task_id"]
    assert (
        client.post("/api/v1/node-api/tasks/poll", headers=auth_header(case.node_token)).status_code
        == 200
    )
    assert (
        report(case, node_task, "complete", {"status": "stopped", "session_id": case.session_id})
        == 200
    )
    done = client.get(
        f"/api/v1/sessions/stop-requests/{request_id}", headers=auth_header(case.token)
    )
    assert done.status_code == 200
    assert done.json()["data"]["result_status"] == "succeeded"


def test_stop_request_conflict_and_owner_scope_do_not_leak_history(client: TestClient) -> None:
    """
    同键异会话固定冲突，其他用户只能看到统一不存在错误。

    :param client (TestClient): 隔离客户端
    """
    first = start_case(client)
    request_id = str(uuid4())
    path = f"/api/v1/sessions/{first.session_id}/stop"
    accepted = client.post(path, headers=auth_header(first.token), json=stop_payload(request_id))
    assert accepted.status_code == 200
    conflict = client.post(
        f"/api/v1/sessions/{first.session_id}/stop",
        headers=auth_header(first.token),
        json={**stop_payload(request_id), "reason": "application_exit"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "SESSION_STOP_REQUEST_CONFLICT"
    hidden = client.get(
        f"/api/v1/sessions/stop-requests/{uuid4()}", headers=auth_header(first.token)
    )
    assert hidden.status_code == 404


def test_stop_request_survives_session_deletion_after_verified_stop(client: TestClient) -> None:
    """
    原会话删除后仍能按同一停止键读取原任务和删除观察。

    :param client (TestClient): 隔离客户端
    """
    case = start_case(client)
    assert report(case, case.task_id, "complete", case.result) == 200
    request_id = str(uuid4())
    response = client.post(
        f"/api/v1/sessions/{case.session_id}/stop",
        headers=auth_header(case.token),
        json=stop_payload(request_id),
    )
    assert response.status_code == 200
    task_id = response.json()["data"]["stop_task_id"]
    assert (
        client.post("/api/v1/node-api/tasks/poll", headers=auth_header(case.node_token)).status_code
        == 200
    )
    assert (
        report(case, task_id, "complete", {"status": "stopped", "session_id": case.session_id})
        == 200
    )
    deleted = client.delete(f"/api/v1/sessions/{case.session_id}", headers=auth_header(case.token))
    assert deleted.status_code == 200, deleted.text
    observed = client.get(
        f"/api/v1/sessions/stop-requests/{request_id}", headers=auth_header(case.token)
    )
    assert observed.status_code == 200
    assert observed.json()["data"]["session_deleted"] is True
    assert observed.json()["data"]["session_id"] == case.session_id


def test_late_create_result_cannot_revive_session_stopped_by_request(client: TestClient) -> None:
    """
    启动阶段停止后迟到创建回报不能把会话恢复为运行中。

    :param client (TestClient): 隔离客户端
    """
    case = start_case(client)
    request_id = str(uuid4())
    response = client.post(
        f"/api/v1/sessions/{case.session_id}/stop",
        headers=auth_header(case.token),
        json=stop_payload(request_id),
    )
    assert response.status_code == 200
    assert current(case)["status"] == "stopping"
    assert report(case, case.task_id, "complete", case.result) == 200
    assert current(case)["status"] == "stopping"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "running"),
        ("session_id", "00000000-0000-4000-8000-000000000001"),
        ("runtime_backend", "foreign_backend"),
        ("runtime_resource_id", "foreign-resource"),
    ],
)
def test_stop_result_must_match_frozen_runtime_identity(
    client: TestClient, field: str, value: str
) -> None:
    """
    普通停止首个成功回报必须与原任务冻结的会话身份一致。

    :param client (TestClient): 隔离客户端
    :param field (str): 要篡改的身份字段
    :param value (str): 篡改后的值
    """
    case = start_case(client)
    request_id = str(uuid4())
    response = client.post(
        f"/api/v1/sessions/{case.session_id}/stop",
        headers=auth_header(case.token),
        json=stop_payload(request_id),
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["data"]["stop_task_id"]
    result = {"status": "stopped", "session_id": case.session_id}
    result[field] = value
    rejected = client.post(
        f"/api/v1/node-api/tasks/{task_id}/complete",
        headers=auth_header(case.node_token),
        json={"result": result},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "SESSION_STOP_RESULT_INVALID"
    assert current(case)["status"] == "stopping"
