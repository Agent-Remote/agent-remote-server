"""
使用 PostgreSQL 验证会话迁移与跨事务刷新互斥。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_port_forward_integration import integration_url

from agent_remote_server.config import Settings, get_settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuthToken, User
from agent_remote_server.schemas.auth import CliSessionTokenData
from agent_remote_server.security import hash_token
from agent_remote_server.services.cli_login_sessions import CliLoginSessionService
from agent_remote_server.services.identity import IdentityService


def test_postgres_refresh_and_logout_share_session_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    真实数据库上并发续期只有一个赢家，滞后的注销仍撤销新凭据。

    :param monkeypatch (pytest.MonkeyPatch): 环境配置工具
    """
    url = integration_url("AGENT_REMOTE_INTEGRATION_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        migration_config = Config()
        migration_config.set_main_option("script_location", "migrations")
        command.upgrade(migration_config, "head")
    finally:
        get_settings.cache_clear()

    async def scenario() -> None:
        """
        执行多事务轮换和注销。
        """
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        settings = Settings(secret_key="test-secret", database_url=url)
        try:
            async with factory() as session:
                user = User(
                    username=f"cli-session-{uuid4()}",
                    display_name="Test",
                    role="user",
                    status="active",
                    password_hash="unused",
                    totp_enabled=False,
                )
                session.add(user)
                await session.flush()
                token = AuthToken(
                    user_id=user.id,
                    token_type="user",
                    status="active",
                    token_hash=hash_token(settings.secret_key, str(uuid4())),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
                session.add(token)
                await session.commit()
                pair = await CliLoginSessionService(session, settings).create(token)

            async def refresh() -> CliSessionTokenData:
                """
                使用独立事务竞争同一个刷新凭据。

                :return CliSessionTokenData: 成功者的新凭据
                """
                async with factory() as session:
                    return await CliLoginSessionService(session, settings).refresh(
                        pair.refresh_token
                    )

            async with factory() as logout_session:
                stale = await logout_session.scalar(
                    select(AuthToken).where(
                        AuthToken.token_hash == hash_token(settings.secret_key, pair.access_token)
                    )
                )
                assert stale is not None
                outcomes = await asyncio.gather(refresh(), refresh(), return_exceptions=True)
                winners = [value for value in outcomes if isinstance(value, CliSessionTokenData)]
                failures = [value for value in outcomes if isinstance(value, ApiError)]
                assert len(winners) == len(failures) == 1
                await IdentityService(logout_session, settings).logout(stale)

            async with factory() as session:
                with pytest.raises(ApiError):
                    await CliLoginSessionService(session, settings).refresh(
                        winners[0].refresh_token
                    )
                active = await session.scalar(
                    select(AuthToken).where(
                        AuthToken.token_hash
                        == hash_token(settings.secret_key, winners[0].access_token)
                    )
                )
                assert active is not None and active.status == "revoked"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
