"""
验证精确节点挂接只使用实际强制命令设备密钥，并保持旧协议兼容。
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from legacy_session_result_support import business_rows
from reviewed_runtime_recovery_support import change_rows
from session_attach_support import attach_case, key_id, verify_body
from test_config_import_requests import client as client
from test_sessions_api import auth_header


@pytest.mark.parametrize("backend", ["native", "docker_sandbox"])
def test_exact_node_verification_echoes_complete_runtime_identity(
    client: TestClient, backend: str
) -> None:
    """
    两种后端均回显四个实际身份和必需可空运行时字段。

    :param client (TestClient): 独立客户端
    :param backend (str): 原运行后端
    """
    case = attach_case(client, backend)
    body = verify_body(case)
    response = client.post(
        "/api/v1/node-api/attach/exact/verify", headers=auth_header(case.node_token), json=body
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == {
        "version",
        "tool_type",
        "node_id",
        "session_id",
        "device_id",
        "ssh_key_id",
        "tmux_session_name",
        "container_id",
        "runtime_backend",
        "runtime_resource_id",
        "forward_ssh_agent",
    }
    assert all(data[key] == value for key, value in body.items())
    assert data["version"] == 1 and data["tool_type"] == "claude"
    assert data["runtime_backend"] == backend and data["forward_ssh_agent"] is False


@pytest.mark.parametrize(
    "invalid",
    [
        "node",
        "session",
        "device",
        "key",
        "missing_key",
        "extra",
        "revoked_key",
        "revoked_device",
        "disabled_owner",
        "stopped",
        "tool",
        "capability",
    ],
)
def test_exact_node_verification_rejects_changed_identity_without_audit(
    client: TestClient, invalid: str
) -> None:
    """
    缺失、撤销或不同设备密钥均在运行时投影及验证审计前拒绝。

    :param client (TestClient): 独立客户端
    :param invalid (str): 身份或当前权限变更
    """
    case = attach_case(client)
    body = verify_body(case)
    if invalid in {"node", "session", "device"}:
        body[f"{invalid}_id"] = str(uuid4())
    elif invalid == "key":
        body["ssh_key_id"] = str(uuid4())
    elif invalid == "missing_key":
        del body["ssh_key_id"]
    elif invalid == "extra":
        body["command"] = "untrusted"
    elif invalid == "revoked_key":
        change_rows(client, "UPDATE ssh_keys SET status='revoked'")
    elif invalid == "revoked_device":
        change_rows(client, "UPDATE user_devices SET status='revoked'")
    elif invalid == "disabled_owner":
        change_rows(client, "UPDATE users SET status='disabled'")
    elif invalid == "stopped":
        change_rows(client, "UPDATE sessions SET status='stopped'")
    elif invalid == "tool":
        change_rows(client, "UPDATE sessions SET tool_type='codex'")
    else:
        change_rows(client, "UPDATE nodes SET runtime_capabilities='{}'")
    before = business_rows(client)
    response = client.post(
        "/api/v1/node-api/attach/exact/verify", headers=auth_header(case.node_token), json=body
    )
    assert response.status_code in {403, 404, 409, 422}, response.text
    assert business_rows(client) == before


def test_legacy_node_verification_keeps_original_response(client: TestClient) -> None:
    """
    旧路由仍接受三身份请求且不意外要求新网关能力或密钥字段。

    :param client (TestClient): 独立客户端
    """
    case = attach_case(client)
    change_rows(client, "UPDATE nodes SET runtime_capabilities='{}'")
    body = verify_body(case)
    del body["ssh_key_id"]
    response = client.post(
        "/api/v1/node-api/attach/verify", headers=auth_header(case.node_token), json=body
    )
    assert response.status_code == 200, response.text
    assert "version" not in response.json()["data"]


def test_actual_key_from_another_device_of_same_user_is_rejected(client: TestClient) -> None:
    """
    同用户另一台设备的有效密钥不能冒充原设备强制命令身份。

    :param client (TestClient): 独立客户端
    """
    case = attach_case(client)
    body = verify_body(case)
    registered = client.post(
        "/api/v1/devices/register",
        headers=auth_header(case.token),
        json={
            "name": "other-device",
            "platform": "linux",
            "ssh_public_key": "ssh-ed25519 AAAAOTHERKEY other@test",
            "wireguard_public_key": "other-device-wireguard",
        },
    )
    assert registered.status_code == 200
    other = registered.json()["data"]
    body["ssh_key_id"] = key_id(case, other["device"]["id"])
    before = business_rows(client)
    response = client.post(
        "/api/v1/node-api/attach/exact/verify", headers=auth_header(case.node_token), json=body
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "SSH_KEY_REVOKED"
    assert business_rows(client) == before
