"""
回归旧协议创建及停止任务的精确重放、冲突回报和迟到状态覆盖。
"""

import pytest
from fastapi.testclient import TestClient
from legacy_session_result_support import business_rows, current, report, start_case
from reviewed_runtime_recovery_support import change_rows
from test_config_import_requests import client as client
from test_sessions_api import auth_header


@pytest.mark.parametrize("outcome", ["complete", "fail"])
@pytest.mark.parametrize("state", ["stopping", "stopped", "interrupted"])
def test_late_create_result_cannot_replace_newer_session_state(
    client: TestClient, outcome: str, state: str
) -> None:
    """
    原启动尚未回报时，较晚的生命周期意图和资源字段不得被旧结果覆盖。

    :param client (TestClient): 独立客户端
    :param outcome (str): 迟到成功或失败
    :param state (str): 已推进的会话状态
    """
    case = start_case(client)
    change_rows(
        client,
        "UPDATE sessions SET status=?, tmux_session_name='newer-terminal' WHERE id=?",
        (state, case.session_id.replace("-", "")),
    )
    before = current(case)
    value: dict[str, object] = case.result if outcome == "complete" else {"code": "START_FAILED"}
    assert report(case, case.task_id, outcome, value) == 200
    assert current(case) == before


@pytest.mark.parametrize("outcome", ["complete", "fail"])
def test_exact_create_replay_does_not_reapply_state_or_business_side_effects(
    client: TestClient, outcome: str
) -> None:
    """
    保存后相同回报只重放原收据，不能复活已经停止的会话。

    :param client (TestClient): 独立客户端
    :param outcome (str): 原始结果类型
    """
    case = start_case(client)
    value: dict[str, object] = case.result if outcome == "complete" else {"code": "START_FAILED"}
    assert report(case, case.task_id, outcome, value) == 200
    change_rows(
        client,
        "UPDATE sessions SET status='stopped' WHERE id=?",
        (case.session_id.replace("-", ""),),
    )
    before = business_rows(client)
    assert report(case, case.task_id, outcome, value) == 200
    assert business_rows(client) == before


@pytest.mark.parametrize(
    "first,second",
    [("complete", "complete"), ("complete", "fail"), ("fail", "complete"), ("fail", "fail")],
)
def test_conflicting_original_result_cannot_change_terminal_receipt(
    client: TestClient, first: str, second: str
) -> None:
    """
    原任务终态之后不同内容或不同结局必须拒绝且保持历史不变。

    :param client (TestClient): 独立客户端
    :param first (str): 原结果类型
    :param second (str): 冲突结果类型
    """
    case = start_case(client)
    value: dict[str, object] = case.result if first == "complete" else {"code": "START_FAILED"}
    assert report(case, case.task_id, first, value) == 200
    changed: dict[str, object] = (
        {**case.result, "status": "active"}
        if second == "complete"
        else {"code": "DIFFERENT_FAILURE"}
    )
    before = business_rows(client)
    assert report(case, case.task_id, second, changed) == 409
    assert business_rows(client) == before


def test_stop_result_replay_cannot_overwrite_later_interrupted_state(client: TestClient) -> None:
    """
    原停止收据重放不改写后续状态，成功之后的失败也不能重新结算。

    :param client (TestClient): 独立客户端
    """
    case = start_case(client)
    assert report(case, case.task_id, "complete", case.result) == 200
    stopped = client.post(
        f"/api/v1/sessions/{case.session_id}/stop", headers=auth_header(case.token)
    )
    assert stopped.status_code == 200
    task = f"stop_tool_session:{case.session_id}"
    assert (
        client.post("/api/v1/node-api/tasks/poll", headers=auth_header(case.node_token)).status_code
        == 200
    )
    result: dict[str, object] = {"status": "stopped", "session_id": case.session_id}
    assert report(case, task, "complete", result) == 200
    change_rows(
        client,
        "UPDATE sessions SET status='interrupted' WHERE id=?",
        (case.session_id.replace("-", ""),),
    )
    before = business_rows(client)
    assert report(case, task, "complete", result) == 200
    assert business_rows(client) == before
    assert report(case, task, "fail", {"code": "LATE_FAILURE"}) == 409
    assert business_rows(client) == before


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "expired"])
def test_terminal_task_without_receipt_rejects_result(client: TestClient, state: str) -> None:
    """
    缺失收据的终态不能重新结算或改变会话。

    :param client (TestClient): 独立客户端
    :param state (str): 已有终态
    """
    case = start_case(client)
    change_rows(client, "UPDATE node_tasks SET status=? WHERE task_id=?", (state, case.task_id))
    before = business_rows(client)
    assert report(case, case.task_id, "complete", case.result) == 409
    assert business_rows(client) == before


def test_json_boolean_is_not_integer_result_replay(client: TestClient) -> None:
    """
    JSON 布尔值与数字即使在 Python 比较相等也不是同一收据。

    :param client (TestClient): 独立客户端
    """
    case = start_case(client)
    result = {**case.result, "marker": True}
    assert report(case, case.task_id, "complete", result) == 200
    before = business_rows(client)
    assert report(case, case.task_id, "complete", {**result, "marker": 1}) == 409
    assert business_rows(client) == before


@pytest.mark.parametrize("payload", ["{}", '{"session_id":"session-test"}'])
def test_unbound_legacy_task_cannot_use_result_session_as_authority(
    client: TestClient, payload: str
) -> None:
    """
    原任务缺少有效会话绑定时，结果中的会话标识不能补足授权。

    :param client (TestClient): 独立客户端
    :param payload (str): 丢失或损坏的原任务会话绑定
    """
    case = start_case(client)
    change_rows(client, "UPDATE node_tasks SET payload=? WHERE task_id=?", (payload, case.task_id))
    before = business_rows(client)
    assert report(case, case.task_id, "complete", case.result) == 409
    assert business_rows(client) == before


def test_deleted_session_replays_receipt_without_recreating_history(client: TestClient) -> None:
    """
    展示行删除后原任务仍可重放，而历史和资源不得重新创建。

    :param client (TestClient): 独立客户端
    """
    case = start_case(client)
    assert report(case, case.task_id, "fail", {"code": "START_FAILED"}) == 200
    deleted = client.delete(f"/api/v1/sessions/{case.session_id}", headers=auth_header(case.token))
    assert deleted.status_code == 200
    before = business_rows(client)
    assert report(case, case.task_id, "fail", {"code": "START_FAILED"}) == 200
    assert business_rows(client) == before


def test_late_original_create_cannot_change_replacement(client: TestClient) -> None:
    """
    显式替换后的旧启动回报只能记录原任务，不能改写任一会话。

    :param client (TestClient): 独立客户端
    """
    case = start_case(client)
    change_rows(
        client,
        "UPDATE sessions SET status='interrupted' WHERE id=?",
        (case.session_id.replace("-", ""),),
    )
    original = current(case)
    replaced = client.post(
        "/api/v1/sessions",
        headers=auth_header(case.token),
        json={
            "tool_type": "claude",
            "tool_account_id": original["tool_account_id"],
            "workspace_id": original["workspace_id"],
            "project_key": original["project_key"],
            "argv": [],
            "replaces_session_id": case.session_id,
        },
    )
    assert replaced.status_code == 200, replaced.text
    replacement = client.get(
        f"/api/v1/sessions/{replaced.json()['data']['id']}", headers=auth_header(case.token)
    ).json()["data"]
    assert report(case, case.task_id, "complete", case.result) == 200
    assert current(case) == original
    observed = client.get(f"/api/v1/sessions/{replacement['id']}", headers=auth_header(case.token))
    assert observed.status_code == 200
    assert observed.json()["data"] == replacement
