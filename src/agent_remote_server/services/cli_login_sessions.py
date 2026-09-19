"""
以有限期刷新凭据轮换 CLI 短期访问令牌。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuditLog, AuthToken, CliLoginSession
from agent_remote_server.repositories import IdentityRepository
from agent_remote_server.repositories.cli_login_sessions import CliLoginSessionRepository
from agent_remote_server.schemas.auth import CliSessionTokenData
from agent_remote_server.security import create_opaque_token, hash_token


class CliLoginSessionService:
    """
    不改变设备身份和浏览器授权的登录续期服务。
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        绑定事务和期限策略。

        :param session (AsyncSession): 数据库事务
        :param settings (Settings): 应用配置
        """
        self.session = session
        self.settings = settings
        self.repository = CliLoginSessionRepository(session)
        self.identity = IdentityRepository(session)

    async def create(self, token: AuthToken) -> CliSessionTokenData:
        """
        将仍有效的普通用户令牌交换为长期登录会话。

        :param token (AuthToken): 已认证用户令牌
        :return CliSessionTokenData: 新凭据对
        """
        locked = await self.repository.lock_token(token.id)
        await self._validate_token(locked)
        assert locked is not None
        if self._remaining(locked.expires_at) <= 0:
            raise self._unauthorized()
        if locked.cli_session_id is not None:
            raise self._unauthorized()
        login = CliLoginSession(
            user_id=locked.user_id,
            access_token_id=locked.id,
            refresh_token_hash=hash_token(
                self.settings.secret_key, create_opaque_token("ar_refresh")
            ),
            expires_at=datetime.now(UTC) + timedelta(seconds=self.settings.cli_session_ttl_seconds),
        )
        await self.repository.add(login)
        locked.cli_session_id = login.id
        result = await self._rotate(login, locked)
        await self.session.commit()
        return result

    async def refresh(self, refresh_token: str) -> CliSessionTokenData:
        """
        原子消费当前刷新凭据并轮换访问令牌。

        :param refresh_token (str): 刷新凭据
        :return CliSessionTokenData: 新凭据对
        """
        login = await self.repository.by_refresh_hash(
            hash_token(self.settings.secret_key, refresh_token)
        )
        if login is None or self._remaining(login.expires_at) <= 0:
            raise self._unauthorized()
        token = await self.repository.lock_token(login.access_token_id)
        await self._validate_token(token)
        assert token is not None
        if token.user_id != login.user_id:
            raise self._unauthorized()
        result = await self._rotate(login, token)
        await self.session.commit()
        return result

    async def revoke(self, token: AuthToken) -> None:
        """
        按稳定会话身份注销，即使请求与续期同时到达。

        :param token (AuthToken): 发起注销的访问令牌
        """
        if token.cli_session_id is None:
            return
        login = await self.repository.lock_session(token.cli_session_id)
        if login is not None:
            login.expires_at = datetime.now(UTC)
            current = await self.repository.lock_token(login.access_token_id)
            if current is not None:
                current.status = "revoked"
                current.revoked_at = datetime.now(UTC)

    async def _validate_token(self, token: AuthToken | None) -> None:
        """
        验证凭据状态和用户状态，不放宽普通接口的过期检查。

        :param token (AuthToken | None): 当前访问令牌
        """
        if token is None or token.status != "active" or token.token_type != "user":
            raise self._unauthorized()
        user = await self.identity.get_user(token.user_id)
        if user is None or user.status != "active":
            raise self._unauthorized()

    async def _rotate(self, login: CliLoginSession, previous: AuthToken) -> CliSessionTokenData:
        """
        替换两个凭据并审计，不延长会话绝对期限。

        :param login (CliLoginSession): 登录会话
        :param previous (AuthToken): 旧访问令牌
        :return CliSessionTokenData: 新凭据对
        """
        remaining = self._remaining(login.expires_at)
        if remaining <= 0:
            raise self._unauthorized()
        expires_in = min(self.settings.access_token_ttl_seconds, remaining)
        access = create_opaque_token("art")
        refresh = create_opaque_token("ar_refresh")
        now = datetime.now(UTC)
        token = await self.identity.add_token(
            AuthToken(
                user_id=previous.user_id,
                cli_session_id=login.id,
                token_hash=hash_token(self.settings.secret_key, access),
                token_type="user",
                status="active",
                expires_at=now + timedelta(seconds=expires_in),
            )
        )
        previous.status = "revoked"
        previous.revoked_at = now
        login.access_token_id = token.id
        login.refresh_token_hash = hash_token(self.settings.secret_key, refresh)
        await self.identity.add_audit_log(
            AuditLog(
                actor_user_id=previous.user_id,
                action="auth.cli_session_rotate",
                target_type="auth_token",
                target_id=str(token.id),
                details={},
            )
        )
        return CliSessionTokenData(
            access_token=access,
            expires_in=expires_in,
            refresh_token=refresh,
            refresh_expires_in=remaining,
        )

    @staticmethod
    def _remaining(value: datetime) -> int:
        """
        计算严格剩余秒数。

        :param value (datetime): 绝对截止时间
        :return int: 剩余秒数
        """
        return int(
            (
                (value if value.tzinfo else value.replace(tzinfo=UTC)) - datetime.now(UTC)
            ).total_seconds()
        )

    @staticmethod
    def _unauthorized() -> ApiError:
        """
        返回不包含凭据的统一认证错误。

        :return ApiError: 登录失效错误
        """
        return ApiError(
            code="AUTH_CLI_SESSION_INVALID",
            message="CLI login session is invalid or expired; log in again.",
            status_code=401,
        )
