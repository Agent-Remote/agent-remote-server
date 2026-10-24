"""
访问工具会话固定集合删除调用方原键及只读观察。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import SessionDeleteRequest


class SessionDeleteRequestRepository:
    """
    提供用户范围固定集合删除受理记录的精确读写。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        初始化仓储。

        :param session (AsyncSession): 当前数据库会话
        """
        self.session = session

    async def get(self, user_id: UUID, request_id: UUID) -> SessionDeleteRequest | None:
        """
        按用户和原键读取删除受理记录。

        :param user_id (UUID): 所有者用户身份
        :param request_id (UUID): 调用方原键
        :return SessionDeleteRequest | None: 原始删除受理记录
        """
        return await self.session.scalar(
            select(SessionDeleteRequest)
            .where(
                SessionDeleteRequest.user_id == user_id,
                SessionDeleteRequest.request_id == request_id,
            )
            .execution_options(populate_existing=True)
        )

    async def get_for_update(self, user_id: UUID, request_id: UUID) -> SessionDeleteRequest | None:
        """
        锁定用户范围原键，串行化删除受理和重放。

        :param user_id (UUID): 所有者用户身份
        :param request_id (UUID): 调用方原键
        :return SessionDeleteRequest | None: 已锁定记录
        """
        return await self.session.scalar(
            select(SessionDeleteRequest)
            .where(
                SessionDeleteRequest.user_id == user_id,
                SessionDeleteRequest.request_id == request_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def add(self, record: SessionDeleteRequest) -> SessionDeleteRequest:
        """
        加入当前事务并刷新账本身份。

        :param record (SessionDeleteRequest): 固定集合删除记录
        :return SessionDeleteRequest: 已刷新记录
        """
        self.session.add(record)
        await self.session.flush()
        return record
