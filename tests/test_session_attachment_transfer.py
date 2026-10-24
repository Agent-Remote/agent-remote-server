"""
验证精确会话附件租约、回执和失效边界。
"""

import json
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from legacy_session_result_support import LegacySessionCase, business_rows
from reviewed_runtime_recovery_support import change_rows
from session_attach_support import attach_case, key_id
from test_config_import_requests import client as client_fixture  # noqa: F401
from test_sessions_api import auth_header


@pytest.fixture
def client(client_fixture: TestClient) -> TestClient:  # noqa: F811
    """
    暴露共享的文件数据库客户端以执行跨连接断言。

    :param client_fixture (TestClient): 共享文件数据库客户端
    :return TestClient: 测试 API 客户端
    """

    return client_fixture


def enable_transfer(case: LegacySessionCase, client: TestClient) -> None:
    """
    在夹具节点心跳中显式启用附件传输能力。

    :param case (LegacySessionCase): 精确会话夹具
    :param client (TestClient): 测试客户端
    """

    change_rows(
        client,
        "UPDATE nodes SET runtime_capabilities=? WHERE id=?",
        (
            json.dumps(
                {
                    "session_attach": {"supported": True, "protocol_version": 1},
                    "session_attachment_transfer": {
                        "supported": True,
                        "protocol_version": 1,
                    },
                }
            ),
            case.node_id.replace("-", ""),
        ),
    )


def admission(case: LegacySessionCase, client: TestClient, lease_id: str | None = None) -> Any:
    """
    提交一份合法的 Node 附件租约请求。

    :param case (LegacySessionCase): 精确会话夹具
    :param client (TestClient): 测试客户端
    :param lease_id (str | None): 可选租约标识
    :return Any: HTTP 响应
    """

    return client.post(
        "/api/v1/node-api/attach/exact/attachments/verify",
        headers=auth_header(case.node_token),
        json={
            "node_id": case.node_id,
            "session_id": case.session_id,
            "device_id": case.device_id,
            "ssh_key_id": key_id(case),
            "lease_id": lease_id or str(uuid4()),
        },
    )


def test_admission_returns_opaque_bounded_lease(client: TestClient) -> None:
    """
    合法租约回显完整身份和限制，绝不返回账户路径。

    :param client (TestClient): 测试客户端
    """

    case = attach_case(client)
    enable_transfer(case, client)
    response = admission(case, client)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["version"] == 1 and data["operation"] == "transfer_lease"
    assert data["node_id"] == case.node_id
    assert data["session_id"] == case.session_id
    assert data["device_id"] == case.device_id
    assert data["ssh_key_id"] == key_id(case)
    assert data["max_bytes"] == 256 * 1024 * 1024 and data["max_entries"] == 32
    assert "/" not in data["runtime_scope"]
    assert "account" not in data


@pytest.mark.parametrize("field", ["node_id", "session_id", "device_id", "ssh_key_id"])
def test_admission_rejects_changed_identity_without_write(client: TestClient, field: str) -> None:
    """
    Node 传入任一错误身份均在创建租约前拒绝。

    :param client (TestClient): 测试客户端
    :param field (str): 要替换的身份字段
    """

    case = attach_case(client)
    enable_transfer(case, client)
    before = business_rows(client)
    body = {
        "node_id": case.node_id,
        "session_id": case.session_id,
        "device_id": case.device_id,
        "ssh_key_id": key_id(case),
        "lease_id": str(uuid4()),
    }
    body[field] = str(uuid4())
    response = client.post(
        "/api/v1/node-api/attach/exact/attachments/verify",
        headers=auth_header(case.node_token),
        json=body,
    )
    assert response.status_code in {403, 404, 409}
    assert business_rows(client) == before


def test_admission_rejects_concurrent_lease_and_replay(client: TestClient) -> None:
    """
    同一会话不能并行持有租约，已用租约标识也不能重放。

    :param client (TestClient): 测试客户端
    """

    case = attach_case(client)
    enable_transfer(case, client)
    lease_id = str(uuid4())
    assert admission(case, client, lease_id).status_code == 200
    assert admission(case, client, str(uuid4())).json()["error"]["code"] == (
        "ATTACHMENT_LEASE_CONCURRENT"
    )
    replay = admission(case, client, lease_id)
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "ATTACHMENT_LEASE_REPLAY"


def test_cleanup_receipt_is_idempotent_and_unknown_is_not_success(client: TestClient) -> None:
    """
    pending/unknown 回执保持可恢复，cleaned 只接受同一公开回执身份。

    :param client (TestClient): 测试客户端
    """

    case = attach_case(client)
    enable_transfer(case, client)
    response = admission(case, client)
    data = response.json()["data"]
    payload = {
        "node_id": case.node_id,
        "session_id": case.session_id,
        "device_id": case.device_id,
        "ssh_key_id": key_id(case),
        "lease_id": data["lease_id"],
        "status": "unknown",
        "removed_batches": 0,
        "receipt_id": str(uuid4()),
    }
    cleanup = client.post(
        "/api/v1/node-api/attach/exact/attachments/cleanup",
        headers=auth_header(case.node_token),
        json=payload,
    )
    assert cleanup.status_code == 200
    assert cleanup.json()["data"]["status"] == "unknown"
    replay = client.post(
        "/api/v1/node-api/attach/exact/attachments/cleanup",
        headers=auth_header(case.node_token),
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["status"] == "unknown"


def test_cleanup_after_device_revocation_is_rejected_and_retained(client: TestClient) -> None:
    """
    设备撤销使租约失效，随后清理结果仍以 rejected 回执留存。

    :param client (TestClient): 测试客户端
    """

    case = attach_case(client)
    enable_transfer(case, client)
    data = admission(case, client).json()["data"]
    revoked = client.post(
        f"/api/v1/devices/{case.device_id}/revoke", headers=auth_header(case.token)
    )
    assert revoked.status_code == 200, revoked.text
    response = client.post(
        "/api/v1/node-api/attach/exact/attachments/cleanup",
        headers=auth_header(case.node_token),
        json={
            "node_id": case.node_id,
            "session_id": case.session_id,
            "device_id": case.device_id,
            "ssh_key_id": key_id(case),
            "lease_id": data["lease_id"],
            "status": "cleaned",
            "removed_batches": 1,
            "receipt_id": str(uuid4()),
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "rejected"


def test_session_stop_invalidates_live_attachment_lease(client: TestClient) -> None:
    """
    会话停止意图一经受理即撤销仍活动的附件租约。

    :param client (TestClient): 文件数据库客户端
    """

    case = attach_case(client)
    enable_transfer(case, client)
    data = admission(case, client).json()["data"]
    stopped = client.post(
        f"/api/v1/sessions/{case.session_id}/stop",
        headers=auth_header(case.token),
    )
    assert stopped.status_code == 200, stopped.text
    cleanup = client.post(
        "/api/v1/node-api/attach/exact/attachments/cleanup",
        headers=auth_header(case.node_token),
        json={
            "node_id": case.node_id,
            "session_id": case.session_id,
            "device_id": case.device_id,
            "ssh_key_id": key_id(case),
            "lease_id": data["lease_id"],
            "status": "cleaned",
            "removed_batches": 0,
            "receipt_id": str(uuid4()),
        },
    )
    assert cleanup.status_code == 200
    assert cleanup.json()["data"]["status"] == "rejected"


def test_expired_lease_cannot_cleanup_and_next_lease_is_fresh(client: TestClient) -> None:
    """
    过期租约不能伪造清理成功，但同一会话可在失效后取得新租约。

    :param client (TestClient): 测试客户端
    """

    case = attach_case(client)
    enable_transfer(case, client)
    first = admission(case, client, str(uuid4())).json()["data"]
    change_rows(
        client,
        "UPDATE session_attachment_leases SET expires_at='2000-01-01 00:00:00' WHERE id=?",
        (first["lease_id"].replace("-", ""),),
    )
    cleanup = client.post(
        "/api/v1/node-api/attach/exact/attachments/cleanup",
        headers=auth_header(case.node_token),
        json={
            "node_id": case.node_id,
            "session_id": case.session_id,
            "device_id": case.device_id,
            "ssh_key_id": key_id(case),
            "lease_id": first["lease_id"],
            "status": "cleaned",
            "removed_batches": 1,
            "receipt_id": str(uuid4()),
        },
    )
    assert cleanup.status_code == 200
    assert cleanup.json()["data"]["status"] == "rejected"
    second = admission(case, client, str(uuid4()))
    assert second.status_code == 200, second.text
    assert second.json()["data"]["lease_id"] != first["lease_id"]
