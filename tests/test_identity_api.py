import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.main import create_app
from agent_remote_server.models import AuditLog, AuthToken, SshKey, UserDevice, WireGuardPeer
from agent_remote_server.security import hash_token
from agent_remote_server.security.totp import generate_totp_code


async def create_schema(app: FastAPI) -> None:
    """
    创建测试数据库 schema

    :param app (FastAPI): FastAPI 应用
    """

    async with app.state.database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        secret_key="test-secret",
        log_level="CRITICAL",
        database_url="sqlite+aiosqlite:///:memory:",
        access_token_ttl_seconds=3600,
        device_token_ttl_seconds=86_400,
    )
    app = create_app(settings)
    asyncio.run(create_schema(app))
    with TestClient(app) as test_client:
        yield test_client


def bootstrap(
    client: TestClient, *, username: str = "admin", password: str = "admin-secret"
) -> str:
    """
    初始化管理员并返回令牌

    :param client (TestClient): 测试客户端
    :param username (str): 用户名
    :param password (str): 密码

    :return str: 访问令牌
    """

    response = client.post(
        "/api/v1/auth/bootstrap",
        json={"username": username, "password": password, "display_name": "Admin"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["token_type"] == "bearer"
    return str(payload["data"]["access_token"])


def auth_header(token: str) -> dict[str, str]:
    """
    创建认证请求头

    :param token (str): 访问令牌

    :return dict: 请求头
    """

    return {"Authorization": f"Bearer {token}"}


async def expire_auth_token(client: TestClient, raw_token: str) -> None:
    """
    将指定测试令牌标记为过期

    :param client (TestClient): 测试客户端
    :param raw_token (str): 原始令牌
    """

    app = cast(FastAPI, client.app)
    async with app.state.session_factory() as session:
        token = await session.scalar(
            select(AuthToken).where(AuthToken.token_hash == hash_token("test-secret", raw_token))
        )
        assert token is not None
        token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


def test_expired_user_token_cannot_refresh(client: TestClient) -> None:
    admin_token = bootstrap(client)
    asyncio.run(expire_auth_token(client, admin_token))

    response = client.post("/api/v1/auth/refresh", headers=auth_header(admin_token))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


def test_bootstrap_login_and_user_management(client: TestClient) -> None:
    initial_status = client.get("/api/v1/auth/bootstrap-status")
    assert initial_status.status_code == 200
    assert initial_status.json()["data"] == {"required": True}

    admin_token = bootstrap(client)

    initialized_status = client.get("/api/v1/auth/bootstrap-status")
    assert initialized_status.status_code == 200
    assert initialized_status.json()["data"] == {"required": False}

    second_bootstrap = client.post(
        "/api/v1/auth/bootstrap",
        json={"username": "root", "password": "admin-secret"},
    )
    assert second_bootstrap.status_code == 409
    assert second_bootstrap.json()["error"]["code"] == "COMMON_CONFLICT"

    me_response = client.get("/api/v1/users/me", headers=auth_header(admin_token))
    assert me_response.status_code == 200
    assert me_response.json()["data"]["role"] == "admin"

    create_response = client.post(
        "/api/v1/users",
        headers=auth_header(admin_token),
        json={
            "username": "alice",
            "password": "alice-secret",
            "display_name": "Alice",
            "role": "user",
        },
    )
    assert create_response.status_code == 200
    assert create_response.json()["data"]["status"] == "active"

    login_response = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "alice-secret"},
    )
    assert login_response.status_code == 200
    user_token = login_response.json()["data"]["access_token"]

    forbidden_response = client.get("/api/v1/users", headers=auth_header(user_token))
    assert forbidden_response.status_code == 403
    assert forbidden_response.json()["error"]["code"] == "COMMON_FORBIDDEN"

    admin_audit_response = client.get("/api/v1/audit-logs", headers=auth_header(admin_token))
    assert admin_audit_response.status_code == 200
    assert any(
        item["action"] == "users.create" for item in admin_audit_response.json()["data"]["items"]
    )

    user_audit_response = client.get("/api/v1/audit-logs", headers=auth_header(user_token))
    assert user_audit_response.status_code == 200
    user_audit_items = user_audit_response.json()["data"]["items"]
    assert [item["action"] for item in user_audit_items] == ["auth.login"]


def test_cli_device_code_login_flow(client: TestClient) -> None:
    admin_token = bootstrap(client)

    start_response = client.post("/api/v1/auth/cli/start")
    assert start_response.status_code == 200
    start_payload = start_response.json()["data"]
    assert start_payload["verification_url"].endswith(f"/cli?code={start_payload['user_code']}")

    early_complete = client.post(
        "/api/v1/auth/cli/complete",
        json={"device_code": start_payload["device_code"]},
    )
    assert early_complete.status_code == 400
    assert early_complete.json()["error"]["code"] == "COMMON_BAD_REQUEST"

    approve_response = client.post(
        "/api/v1/auth/cli/approve",
        headers=auth_header(admin_token),
        json={"user_code": start_payload["user_code"]},
    )
    assert approve_response.status_code == 200

    complete_response = client.post(
        "/api/v1/auth/cli/complete",
        json={"device_code": start_payload["device_code"]},
    )
    assert complete_response.status_code == 200
    assert complete_response.json()["data"]["access_token"].startswith("art_")


def test_totp_setup_verify_and_login(client: TestClient) -> None:
    admin_token = bootstrap(client)

    setup_response = client.post("/api/v1/auth/totp/setup", headers=auth_header(admin_token))
    assert setup_response.status_code == 200
    secret = setup_response.json()["data"]["secret"]

    verify_response = client.post(
        "/api/v1/auth/totp/verify",
        headers=auth_header(admin_token),
        json={"code": generate_totp_code(secret)},
    )
    assert verify_response.status_code == 200

    missing_totp = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin-secret"},
    )
    assert missing_totp.status_code == 401
    assert missing_totp.json()["error"]["code"] == "AUTH_TOTP_REQUIRED"

    login_response = client.post(
        "/api/v1/auth/login",
        json={
            "username": "admin",
            "password": "admin-secret",
            "totp_code": generate_totp_code(secret),
        },
    )
    assert login_response.status_code == 200


def test_device_registration_revoke_and_audit_sanitization(client: TestClient) -> None:
    admin_token = bootstrap(client)
    ssh_public_key = "ssh-ed25519 AAAATESTKEY rem@test"
    wireguard_public_key = "wg-public-key"

    register_response = client.post(
        "/api/v1/devices/register",
        headers=auth_header(admin_token),
        json={
            "name": "rem-macbook",
            "platform": "macos",
            "ssh_public_key": ssh_public_key,
            "wireguard_public_key": wireguard_public_key,
        },
    )
    assert register_response.status_code == 200
    registration = register_response.json()["data"]
    device_id = UUID(registration["device"]["id"])
    device_token = registration["device_token"]["access_token"]
    assert registration["device"]["last_seen_at"] is not None
    assert registration["device_token"]["expires_in"] == 86_400
    assert registration["ssh_key_id"]
    assert registration["wireguard_peer_id"]

    device_me = client.get("/api/v1/users/me", headers=auth_header(device_token))
    assert device_me.status_code == 200

    refresh_response = client.post("/api/v1/auth/refresh", headers=auth_header(device_token))
    assert refresh_response.status_code == 200
    assert refresh_response.json()["data"]["expires_in"] == 86_400
    refreshed_device_token = refresh_response.json()["data"]["access_token"]
    expired_device_me = client.get("/api/v1/users/me", headers=auth_header(device_token))
    assert expired_device_me.status_code == 401
    assert expired_device_me.json()["error"]["code"] == "AUTH_TOKEN_REVOKED"
    refreshed_device_me = client.get(
        "/api/v1/users/me", headers=auth_header(refreshed_device_token)
    )
    assert refreshed_device_me.status_code == 200

    asyncio.run(expire_auth_token(client, refreshed_device_token))
    expired_refresh = client.post(
        "/api/v1/auth/refresh", headers=auth_header(refreshed_device_token)
    )
    assert expired_refresh.status_code == 200
    refreshed_device_token = expired_refresh.json()["data"]["access_token"]

    revoke_response = client.post(
        f"/api/v1/devices/{device_id}/disable",
        headers=auth_header(admin_token),
    )
    assert revoke_response.status_code == 200

    revoked_token_response = client.get(
        "/api/v1/users/me", headers=auth_header(refreshed_device_token)
    )
    assert revoked_token_response.status_code == 401
    assert revoked_token_response.json()["error"]["code"] == "AUTH_TOKEN_REVOKED"

    async def inspect_state() -> None:
        app = cast(FastAPI, client.app)
        session_factory = app.state.session_factory
        async with session_factory() as session:
            device = await session.get(UserDevice, device_id)
            assert device is not None
            assert device.status == "revoked"
            assert device.last_seen_at is not None

            ssh_keys = list(
                await session.scalars(select(SshKey).where(SshKey.user_device_id == device.id))
            )
            assert [ssh_key.status for ssh_key in ssh_keys] == ["revoked"]

            peers = list(
                await session.scalars(
                    select(WireGuardPeer).where(WireGuardPeer.user_device_id == device.id)
                )
            )
            assert [peer.status for peer in peers] == ["revoked"]

            tokens = list(
                await session.scalars(
                    select(AuthToken).where(AuthToken.user_device_id == device.id)
                )
            )
            assert len(tokens) == 3
            assert all(token.status == "revoked" for token in tokens)

            logs = list(await session.scalars(select(AuditLog)))
            details_text = json.dumps([log.details for log in logs], sort_keys=True)
            assert "admin-secret" not in details_text
            assert device_token not in details_text
            assert ssh_public_key not in details_text
            assert wireguard_public_key not in details_text

    asyncio.run(inspect_state())


def test_device_activity_updates_last_seen_with_write_throttling(client: TestClient) -> None:
    admin_token = bootstrap(client)
    register_response = client.post(
        "/api/v1/devices/register",
        headers=auth_header(admin_token),
        json={
            "name": "activity-device",
            "platform": "linux",
            "ssh_public_key": "ssh-ed25519 AAAADEVICEACTIVITY rem@test",
        },
    )
    assert register_response.status_code == 200
    registration = register_response.json()["data"]
    device_id = UUID(registration["device"]["id"])
    device_token = registration["device_token"]["access_token"]
    stale_seen_at = datetime.now(UTC) - timedelta(minutes=10)

    async def set_last_seen_at(value: datetime) -> None:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            device = await session.get(UserDevice, device_id)
            assert device is not None
            device.last_seen_at = value
            await session.commit()

    async def get_last_seen_at() -> datetime:
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            device = await session.get(UserDevice, device_id)
            assert device is not None
            assert device.last_seen_at is not None
            return device.last_seen_at

    asyncio.run(set_last_seen_at(stale_seen_at))
    activity_response = client.get("/api/v1/users/me", headers=auth_header(device_token))
    assert activity_response.status_code == 200
    refreshed_seen_at = asyncio.run(get_last_seen_at())
    normalized_refreshed = (
        refreshed_seen_at
        if refreshed_seen_at.tzinfo is not None
        else refreshed_seen_at.replace(tzinfo=UTC)
    )
    assert normalized_refreshed > stale_seen_at

    second_activity = client.get("/api/v1/users/me", headers=auth_header(device_token))
    assert second_activity.status_code == 200
    assert asyncio.run(get_last_seen_at()) == refreshed_seen_at


def test_device_delete_requires_revoke(client: TestClient) -> None:
    admin_token = bootstrap(client)
    response = client.post(
        "/api/v1/devices/register",
        headers=auth_header(admin_token),
        json={
            "name": "disposable-device",
            "platform": "macos",
            "ssh_public_key": "ssh-ed25519 AAAADISPOSABLE test@example.com",
        },
    )
    assert response.status_code == 200
    device_id = response.json()["data"]["device"]["id"]

    blocked = client.delete(f"/api/v1/devices/{device_id}", headers=auth_header(admin_token))
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "DEVICE_DELETE_REQUIRES_REVOKED"

    assert (
        client.post(
            f"/api/v1/devices/{device_id}/disable", headers=auth_header(admin_token)
        ).status_code
        == 200
    )
    deleted = client.delete(f"/api/v1/devices/{device_id}", headers=auth_header(admin_token))
    assert deleted.status_code == 200
    assert (
        client.get(f"/api/v1/devices/{device_id}", headers=auth_header(admin_token)).status_code
        == 404
    )
