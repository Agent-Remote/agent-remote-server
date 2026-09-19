"""
验证 CLI 登录续期的有效期、轮换和撤销边界。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_identity_api import auth_header, bootstrap, expire_auth_token
from test_identity_api import client as client

from agent_remote_server.models import AuthToken, CliLoginSession, User
from agent_remote_server.security import hash_token


def create_login_session(client: TestClient) -> dict[str, object]:
    """
    用有效用户令牌换取 CLI 会话。

    :param client (TestClient): 测试客户端
    :return dict[str, object]: 凭据响应
    """
    token = bootstrap(client)
    response = client.post("/api/v1/auth/cli/session", headers=auth_header(token))
    assert response.status_code == 200
    assert client.get("/api/v1/users/me", headers=auth_header(token)).status_code == 401
    return cast(dict[str, object], response.json()["data"])


def test_refresh_after_access_expiry_rotates_both_credentials(client: TestClient) -> None:
    """
    访问令牌过期后仍能续期，旧凭据不能重放。

    :param client (TestClient): 测试客户端
    """
    pair = create_login_session(client)
    access, refresh = str(pair["access_token"]), str(pair["refresh_token"])
    asyncio.run(expire_auth_token(client, access))
    assert client.get("/api/v1/users/me", headers=auth_header(access)).status_code == 401
    assert client.get("/api/v1/users/me", headers=auth_header(refresh)).status_code == 401
    response = client.post("/api/v1/auth/cli/refresh", json={"refresh_token": refresh})
    assert response.status_code == 200
    renewed = response.json()["data"]
    assert renewed["access_token"] != access
    assert renewed["refresh_token"] != refresh
    assert 0 < renewed["refresh_expires_in"] <= pair["refresh_expires_in"]
    assert (
        client.get("/api/v1/users/me", headers=auth_header(renewed["access_token"])).status_code
        == 200
    )
    assert (
        client.post("/api/v1/auth/cli/refresh", json={"refresh_token": refresh}).status_code == 401
    )
    assert (
        client.post("/api/v1/auth/cli/refresh", json={"refresh_token": access}).status_code == 401
    )


@pytest.mark.parametrize("reason", ["logout", "expired_session", "disabled_user", "revoked_token"])
def test_session_cannot_outlive_revocation(client: TestClient, reason: str) -> None:
    """
    会话过期或身份被撤销后不能续期。

    :param client (TestClient): 测试客户端
    :param reason (str): 拒绝原因
    """
    pair = create_login_session(client)

    async def invalidate() -> None:
        """
        设置目标失效状态。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            login = await session.scalar(select(CliLoginSession))
            assert login is not None
            if reason == "expired_session":
                login.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif reason == "disabled_user":
                user = await session.get(User, login.user_id)
                assert user is not None
                user.status = "disabled"
            else:
                token = await session.get(AuthToken, login.access_token_id)
                assert token is not None
                token.status = "revoked"
            await session.commit()

    if reason == "logout":
        assert (
            client.post(
                "/api/v1/auth/logout", headers=auth_header(str(pair["access_token"]))
            ).status_code
            == 200
        )
    else:
        asyncio.run(invalidate())
    assert (
        client.post(
            "/api/v1/auth/cli/refresh", json={"refresh_token": pair["refresh_token"]}
        ).status_code
        == 401
    )


def test_session_cannot_be_recreated_or_refreshed_with_access_only(client: TestClient) -> None:
    """
    短期令牌不能延长会话绝对期限或替代刷新凭据。

    :param client (TestClient): 测试客户端
    """
    pair = create_login_session(client)
    headers = auth_header(str(pair["access_token"]))
    assert client.post("/api/v1/auth/cli/session", headers=headers).status_code == 401
    assert client.post("/api/v1/auth/refresh", headers=headers).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/cli/refresh", json={"refresh_token": pair["refresh_token"]}
        ).status_code
        == 200
    )


def test_session_stores_hashes_only(client: TestClient) -> None:
    """
    数据库不保留刷新凭据明文。

    :param client (TestClient): 测试客户端
    """
    pair = create_login_session(client)

    async def inspect_hash() -> None:
        """
        检查刷新哈希和访问期限。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            login = await session.scalar(select(CliLoginSession))
            assert login is not None
            assert login.refresh_token_hash == hash_token("test-secret", str(pair["refresh_token"]))
            assert login.refresh_token_hash != pair["refresh_token"]

    asyncio.run(inspect_hash())
