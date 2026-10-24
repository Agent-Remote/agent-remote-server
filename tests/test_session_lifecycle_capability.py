"""
验证生命周期心跳的严格后端能力、撤销及旧报告兼容。
"""

from fastapi.testclient import TestClient
from test_config_import_requests import client as client
from test_sessions_api import auth_header, bootstrap, create_node


def heartbeat(
    client: TestClient,
    node_id: str,
    node_token: str,
    runtime_capabilities: dict[str, object],
) -> None:
    """
    经真实节点认证入口发布当前能力快照。

    :param client (TestClient): 独立测试客户端
    :param node_id (str): 已登记节点身份
    :param node_token (str): 合成测试节点凭据
    :param runtime_capabilities (dict[str, object]): 本次完整能力快照
    """
    response = client.post(
        "/api/v1/node-api/heartbeat",
        headers=auth_header(node_token),
        json={
            "node_id": node_id,
            "version": "fixture",
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
                "runtime_capabilities": runtime_capabilities,
            },
        },
    )
    assert response.status_code == 200, response.text


def observed(client: TestClient, token: str, node_id: str) -> dict[str, object]:
    """
    通过管理员节点详情读取已持久化的规范能力。

    :param client (TestClient): 独立测试客户端
    :param token (str): 合成管理员凭据
    :param node_id (str): 原节点身份
    :return dict[str, object]: 服务端持久能力投影
    """
    response = client.get(f"/api/v1/nodes/{node_id}", headers=auth_header(token))
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()["data"]["runtime_capabilities"]
    return result


def test_lifecycle_backend_type_drift_revokes_previous_heartbeat(client: TestClient) -> None:
    """
    后续错误类型不能利用 Python 等值比较保留此前的后端支持。

    :param client (TestClient): 隔离数据库客户端
    """
    token = bootstrap(client)
    node_id, node_token = create_node(client, token, name="lifecycle-fixture", weight=10)
    supported = {"supported": True, "protocol_version": 1}
    invalid_entries: list[object] = [
        None,
        True,
        [],
        {},
        {"supported": True},
        {"protocol_version": 1},
        {"supported": False, "protocol_version": 1},
        {"supported": 1, "protocol_version": 1},
        {"supported": "true", "protocol_version": 1},
        {"supported": True, "protocol_version": True},
        {"supported": True, "protocol_version": 1.0},
        {"supported": True, "protocol_version": "1"},
        {"supported": True, "protocol_version": 2},
        {"supported": True, "protocol_version": 1, "stopped": True},
        {"Supported": True, "protocol_version": 1},
    ]
    for backend, other in (("native", "docker_sandbox"), ("docker_sandbox", "native")):
        for invalid in invalid_entries:
            heartbeat(
                client,
                node_id,
                node_token,
                {"session_lifecycle": {backend: supported, other: supported}},
            )
            assert observed(client, token, node_id)["session_lifecycle"] == {
                backend: supported,
                other: supported,
            }
            heartbeat(
                client,
                node_id,
                node_token,
                {
                    "session_lifecycle": {backend: invalid, other: supported},
                    "binding_attach": supported,
                },
            )
            result = observed(client, token, node_id)
            assert result["session_lifecycle"] == {other: supported}, (backend, invalid)
            assert result["binding_attach"] == supported


def test_lifecycle_missing_reports_and_unknown_backends_confer_no_support(
    client: TestClient,
) -> None:
    """
    旧节点或未知后端报告撤销原能力，其他既有能力保持独立。

    :param client (TestClient): 隔离数据库客户端
    """
    token = bootstrap(client)
    node_id, node_token = create_node(client, token, name="legacy-fixture", weight=10)
    supported = {"supported": True, "protocol_version": 1}
    invalid_reports: list[object] = [None, True, [], "native", {}]
    for invalid in invalid_reports:
        heartbeat(client, node_id, node_token, {"session_lifecycle": {"native": supported}})
        heartbeat(client, node_id, node_token, {"session_lifecycle": invalid})
        assert observed(client, token, node_id)["session_lifecycle"] == {}
    heartbeat(
        client,
        node_id,
        node_token,
        {"session_lifecycle": {"native": supported, "future": supported}},
    )
    assert observed(client, token, node_id)["session_lifecycle"] == {"native": supported}
    heartbeat(client, node_id, node_token, {"session_lifecycle": {"Native": supported}})
    assert observed(client, token, node_id)["session_lifecycle"] == {}
    heartbeat(client, node_id, node_token, {"session_lifecycle": {"native": supported}})
    heartbeat(client, node_id, node_token, {"session_attach": supported})
    result = observed(client, token, node_id)
    assert "session_lifecycle" not in result
    assert result["session_attach"] == supported
