"""
访问设备登记调用方原键。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import DeviceRegistrationRequest


class DeviceRegistrationRequestRepository:
    """
    提供用户范围原键的精确读写。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        初始化仓储。

        :param session (AsyncSession): 当前数据库会话
        """

        self._session = session

    async def get_for_update(
        self, user_id: UUID, request_id: UUID
    ) -> DeviceRegistrationRequest | None:
        """
        锁定一条登记请求，串行化重放。

        :param user_id (UUID): 所有者用户身份
        :param request_id (UUID): 调用方原键
        :return DeviceRegistrationRequest | None: 原键记录
        """

        return await self._session.scalar(
            select(DeviceRegistrationRequest)
            .where(
                DeviceRegistrationRequest.user_id == user_id,
                DeviceRegistrationRequest.request_id == request_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def get(self, user_id: UUID, request_id: UUID) -> DeviceRegistrationRequest | None:
        """
        按用户和原键读取登记记录，不创建或更新任何状态。

        :param user_id (UUID): 所有者用户身份
        :param request_id (UUID): 调用方原键
        :return DeviceRegistrationRequest | None: 原键记录
        """

        return await self._session.scalar(
            select(DeviceRegistrationRequest).where(
                DeviceRegistrationRequest.user_id == user_id,
                DeviceRegistrationRequest.request_id == request_id,
            )
        )

    async def add(self, record: DeviceRegistrationRequest) -> DeviceRegistrationRequest:
        """
        新增登记请求并刷新主键。

        :param record (DeviceRegistrationRequest): 登记请求记录
        :return DeviceRegistrationRequest: 已刷新的记录
        """

        self._session.add(record)
        await self._session.flush()
        return record
