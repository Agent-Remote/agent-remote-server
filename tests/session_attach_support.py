"""
建立真实旧会话入口，支持精确挂接协议的隔离 HTTP 回归。
"""

import json
import sqlite3
from contextlib import closing
from uuid import UUID

from config_import_request_support import database_path
from fastapi.testclient import TestClient
from legacy_session_result_support import LegacySessionCase, report, start_case
from reviewed_runtime_recovery_support import change_rows
from test_sessions_api import auth_header


def attach_case(client: TestClient, backend: str = "native") -> LegacySessionCase:
    """
    完成原启动并显式声明安装网关支持精确协议。

    :param client (TestClient): 文件数据库客户端
    :param backend (str): 合成运行时后端
    :return LegacySessionCase: 可挂接原始会话
    """
    case = start_case(client)
    assert report(case, case.task_id, "complete", case.result) == 200
    change_rows(
        client,
        "UPDATE nodes SET runtime_capabilities=? WHERE id=?",
        (
            json.dumps({"session_attach": {"supported": True, "protocol_version": 1}}),
            case.node_id.replace("-", ""),
        ),
    )
    change_rows(
        client,
        "UPDATE sessions SET runtime_backend=? WHERE id=?",
        (backend, case.session_id.replace("-", "")),
    )
    change_rows(client, "UPDATE user_devices SET last_seen_at='2000-01-01 00:00:00'")
    return case


def path(case: LegacySessionCase) -> str:
    """
    返回原会话精确授权路径。

    :param case (LegacySessionCase): 原始上下文
    :return str: 授权路径
    """
    return f"/api/v1/sessions/{case.session_id}/attach/exact"


def authorize(case: LegacySessionCase) -> dict[str, object]:
    """
    单次显式授权并返回严格响应对象。

    :param case (LegacySessionCase): 原始上下文
    :return dict[str, object]: 授权结果
    """
    response = case.client.post(
        path(case),
        headers=auth_header(case.device_token),
        json={"expected_device_id": case.device_id},
    )
    assert response.status_code == 200, response.text
    data: dict[str, object] = response.json()["data"]
    return data


def key_id(case: LegacySessionCase, device_id: str | None = None) -> str:
    """
    读取夹具中的实际设备密钥行标识，不读取密钥内容。

    :param case (LegacySessionCase): 原始上下文
    :param device_id (str | None): 可选另一台设备身份
    :return str: 实际密钥 UUID
    """
    with closing(sqlite3.connect(database_path(case.client))) as connection:
        row = connection.execute(
            "SELECT id FROM ssh_keys WHERE user_device_id=?",
            ((device_id or case.device_id).replace("-", ""),),
        ).fetchone()
    assert row is not None
    return str(UUID(row[0]))


def verify_body(case: LegacySessionCase) -> dict[str, str]:
    """
    模拟可信强制命令的实际身份投影。

    :param case (LegacySessionCase): 原始上下文
    :return dict[str, str]: 四个精确 UUID
    """
    return {
        "node_id": case.node_id,
        "session_id": case.session_id,
        "device_id": case.device_id,
        "ssh_key_id": key_id(case),
    }
