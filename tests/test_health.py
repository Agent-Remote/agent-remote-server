"""
验证健康状态行为。
"""

from fastapi.testclient import TestClient

from agent_remote_server.config import Settings
from agent_remote_server.main import create_app


def make_client() -> TestClient:
    """
    设置客户端。

    :return TestClient: 客户端
    """
    settings = Settings(
        secret_key="test-secret",
        log_level="CRITICAL",
        database_url="sqlite+aiosqlite://",
    )
    return TestClient(create_app(settings))


def test_healthz_returns_process_health() -> None:
    """
    验证健康检查返回进程健康状态。
    """
    with make_client() as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["x-request-id"].startswith("req_")
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "agent-remote-server"
    assert payload["components"]["process"]["status"] == "ok"


def test_request_id_header_is_preserved() -> None:
    """
    验证请求 ID 请求头为保留。
    """
    with make_client() as client:
        response = client.get("/healthz", headers={"x-request-id": "req_test"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req_test"
    assert response.json()["request_id"] == "req_test"


def test_version_endpoint() -> None:
    """
    验证版本端点。
    """
    with make_client() as client:
        response = client.get("/api/v1/version")

    assert response.status_code == 200
    assert response.json()["data"]["service"] == "agent-remote-server"
