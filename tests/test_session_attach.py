"""
验证精确 Claude 会话授权的设备绑定、只读轮询和旧网关拒绝边界。
"""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from legacy_session_result_support import business_rows
from reviewed_runtime_recovery_support import change_rows
from session_attach_support import attach_case, authorize, path
from test_config_import_requests import client as client
from test_sessions_api import auth_header


@pytest.mark.parametrize("backend", ["native", "docker_sandbox"])
@pytest.mark.parametrize("state", ["running", "active"])
def test_exact_attach_polls_original_row_without_any_write(
    client: TestClient, backend: str, state: str
) -> None:
    """
    两种后端与可挂接状态均返回固定设备身份，重复 GET 不写任何业务行。

    :param client (TestClient): 独立客户端
    :param backend (str): 运行后端
    :param state (str): 可挂接状态
    """
    case = attach_case(client, backend)
    change_rows(client, "UPDATE sessions SET status=?", (state,))
    data = authorize(case)
    assert set(data) == {
        "version",
        "tool_type",
        "session_id",
        "node_id",
        "device_id",
        "ssh_host",
        "ssh_port",
        "ssh_user",
        "tmux_session_name",
        "forward_ssh_agent",
        "authorization_task_id",
        "authorization_task_record_id",
        "authorization_task_status",
        "expires_in",
    }
    assert data["version"] == 1 and data["tool_type"] == "claude"
    assert data["session_id"] == case.session_id and data["device_id"] == case.device_id
    assert data["forward_ssh_agent"] is False
    assert data["authorization_task_status"] == "pending"
    before = business_rows(client)
    for _ in range(2):
        response = client.get(
            f"{path(case)}/{data['authorization_task_id']}",
            params={
                "expected_device_id": case.device_id,
                "expected_task_record_id": str(data["authorization_task_record_id"]),
            },
            headers=auth_header(case.device_token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] == data
    assert business_rows(client) == before


@pytest.mark.parametrize(
    "capability",
    [
        {"supported": 1, "protocol_version": 1},
        {"supported": True, "protocol_version": True},
        {"supported": True, "protocol_version": 1.0},
    ],
)
def test_session_gateway_heartbeat_type_drift_clears_previous_support(
    client: TestClient, capability: dict[str, object]
) -> None:
    """
    类型漂移不能因 Python 等值比较而保留先前的已安装网关能力。

    :param client (TestClient): 独立客户端
    :param capability (dict[str, object]): 类型不精确的后续网关报告
    """
    case = attach_case(client)
    response = client.post(
        "/api/v1/node-api/heartbeat",
        headers=auth_header(case.node_token),
        json={
            "node_id": case.node_id,
            "version": "test",
            "supported_tool_types": ["claude"],
            "resources": {
                "cpu_load": 0.0,
                "memory_used_bytes": 0,
                "memory_total_bytes": 1,
                "disk_used_bytes": 0,
                "disk_total_bytes": 1,
            },
            "runtime": {
                "docker_ok": True,
                "tmux_ok": True,
                "runtime_capabilities": {
                    "backends": ["native", "docker_sandbox"],
                    "session_attach": capability,
                },
            },
        },
    )
    assert response.status_code == 200, response.text
    before = business_rows(client)
    denied = client.post(
        path(case),
        headers=auth_header(case.device_token),
        json={"expected_device_id": case.device_id},
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "NODE_SESSION_ATTACH_UNSUPPORTED"
    assert business_rows(client) == before


@pytest.mark.parametrize("invalid", ["device", "user_token", "tool", "starting", "extra"])
def test_exact_attach_rejects_before_any_write(client: TestClient, invalid: str) -> None:
    """
    设备不符、非设备凭据、其他工具和未运行会话均无副作用。

    :param client (TestClient): 独立客户端
    :param invalid (str): 无效请求类别
    """
    case = attach_case(client)
    body = {"expected_device_id": case.device_id}
    token = case.device_token
    if invalid == "device":
        body["expected_device_id"] = str(uuid4())
    elif invalid == "user_token":
        token = case.token
    elif invalid == "tool":
        change_rows(client, "UPDATE sessions SET tool_type='codex'")
    elif invalid == "starting":
        change_rows(client, "UPDATE sessions SET status='starting'")
    else:
        body["command"] = "untrusted"
    before = business_rows(client)
    response = client.post(path(case), headers=auth_header(token), json=body)
    assert response.status_code in {403, 409, 422}, response.text
    assert business_rows(client) == before


@pytest.mark.parametrize(
    "capability",
    [
        None,
        {},
        {"supported": 1, "protocol_version": 1},
        {"supported": True, "protocol_version": True},
        {"supported": True, "protocol_version": 1.0},
        {"supported": True, "protocol_version": 2},
        {"supported": False, "protocol_version": 1},
    ],
)
def test_exact_attach_requires_current_gateway_capability(
    client: TestClient, capability: object
) -> None:
    """
    缺失或类型不精确的网关声明必须在授权任务创建前拒绝。

    :param client (TestClient): 独立客户端
    :param capability (object): 缺失或无效能力值
    """
    case = attach_case(client)
    change_rows(
        client,
        "UPDATE nodes SET runtime_capabilities=?",
        (json.dumps({"session_attach": capability}),),
    )
    before = business_rows(client)
    response = client.post(
        path(case),
        headers=auth_header(case.device_token),
        json={"expected_device_id": case.device_id},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NODE_SESSION_ATTACH_UNSUPPORTED"
    assert business_rows(client) == before


@pytest.mark.parametrize(
    "change",
    [
        "failed",
        "row",
        "logical",
        "payload",
        "type",
        "missing",
        "device",
        "key",
        "stopped",
        "tool",
        "node",
        "owner",
        "capability",
    ],
)
def test_exact_attach_observation_does_not_repair_changed_authority(
    client: TestClient, change: str
) -> None:
    """
    失败原任务仅被观察，身份或权限变化不会触发重置、重新授权或活动写入。

    :param client (TestClient): 独立客户端
    :param change (str): 模拟变化
    """
    case = attach_case(client)
    data = authorize(case)
    logical = str(data["authorization_task_id"])
    record = str(data["authorization_task_record_id"])
    device = case.device_id
    if change == "failed":
        change_rows(client, "UPDATE node_tasks SET status='failed' WHERE task_id=?", (logical,))
    elif change == "row":
        record = str(uuid4())
    elif change == "logical":
        logical = "wrong-original"
    elif change == "payload":
        change_rows(client, "UPDATE node_tasks SET payload='{}' WHERE task_id=?", (logical,))
    elif change == "type":
        change_rows(
            client, "UPDATE node_tasks SET task_type='unrelated' WHERE task_id=?", (logical,)
        )
    elif change == "missing":
        change_rows(client, "DELETE FROM node_tasks WHERE task_id=?", (logical,))
    elif change == "device":
        device = str(uuid4())
    elif change == "key":
        change_rows(client, "UPDATE ssh_keys SET status='revoked'")
    elif change == "stopped":
        change_rows(client, "UPDATE sessions SET status='stopped'")
    elif change == "tool":
        change_rows(client, "UPDATE sessions SET tool_type='codex'")
    elif change == "node":
        change_rows(client, "UPDATE nodes SET status='offline'")
    elif change == "owner":
        change_rows(client, "UPDATE users SET status='disabled'")
    else:
        change_rows(client, "UPDATE nodes SET runtime_capabilities='{}'")
    before = business_rows(client)
    response = client.get(
        f"{path(case)}/{logical}",
        headers=auth_header(case.device_token),
        params={"expected_device_id": device, "expected_task_record_id": record},
    )
    if change == "failed":
        assert response.status_code == 200, response.text
        assert response.json()["data"]["authorization_task_status"] == "failed"
    else:
        assert response.status_code in {401, 403, 404, 409}, response.text
    assert business_rows(client) == before
