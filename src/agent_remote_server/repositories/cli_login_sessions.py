"""
提供 CLI 登录会话的事务锁与持久化操作。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import AuthToken, CliLoginSession


class CliLoginSessionRepository:
    """
    刷新会话数据仓库。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定请求事务。

        :param session (AsyncSession): 数据库会话
        """
        self.session = session

    async def by_refresh_hash(self, value: str) -> CliLoginSession | None:
        """
        锁定刷新凭据对应的当前会话。

        :param value (str): 刷新凭据哈希
        :return CliLoginSession | None: 会话记录
        """
        return await self.session.scalar(
            select(CliLoginSession)
            .where(CliLoginSession.refresh_token_hash == value)
            .with_for_update()
        )

    async def lock_token(self, token_id: UUID) -> AuthToken | None:
        """
        锁定令牌并重新读取撤销状态。

        :param token_id (UUID): 令牌 ID
        :return AuthToken | None: 当前令牌
        """
        return await self.session.scalar(
            select(AuthToken)
            .where(AuthToken.id == token_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def lock_session(self, session_id: UUID) -> CliLoginSession | None:
        """
        按稳定会话 ID 锁定当前刷新状态。

        :param session_id (UUID): 会话 ID
        :return CliLoginSession | None: 会话记录
        """
        return await self.session.scalar(
            select(CliLoginSession)
            .where(CliLoginSession.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def add(self, login: CliLoginSession) -> None:
        """
        保存新会话。

        :param login (CliLoginSession): 会话记录
        """
        self.session.add(login)
        await self.session.flush()
