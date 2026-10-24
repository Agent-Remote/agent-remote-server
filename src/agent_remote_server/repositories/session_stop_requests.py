"""
访问工具会话停止调用方原键及只读观察。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask, NodeTaskResult, Session, SessionStopRequest


class SessionStopRequestRepository:
    """
    提供用户范围停止受理记录的精确读写。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        初始化仓储。

        :param session (AsyncSession): 当前数据库会话
        """
        self.session = session

    async def get(self, user_id: UUID, request_id: UUID) -> SessionStopRequest | None:
        """
        按用户和原键读取停止受理记录。

        :param user_id (UUID): 所有者用户身份
        :param request_id (UUID): 调用方原键
        :return SessionStopRequest | None: 原始受理记录
        """
        return await self.session.scalar(
            select(SessionStopRequest)
            .where(
                SessionStopRequest.user_id == user_id,
                SessionStopRequest.request_id == request_id,
            )
            .execution_options(populate_existing=True)
        )

    async def get_for_update(self, user_id: UUID, request_id: UUID) -> SessionStopRequest | None:
        """
        锁定用户范围原键，串行化停止受理和重放。

        :param user_id (UUID): 所有者用户身份
        :param request_id (UUID): 调用方原键
        :return SessionStopRequest | None: 已锁定记录
        """
        return await self.session.scalar(
            select(SessionStopRequest)
            .where(
                SessionStopRequest.user_id == user_id,
                SessionStopRequest.request_id == request_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def add(self, record: SessionStopRequest) -> SessionStopRequest:
        """
        加入当前事务并刷新数据库生成身份。

        :param record (SessionStopRequest): 原始受理记录
        :return SessionStopRequest: 已刷新记录
        """
        self.session.add(record)
        await self.session.flush()
        return record

    async def task(self, task_id: str) -> NodeTask | None:
        """
        读取原受理记录绑定的精确节点任务。

        :param task_id (str): 节点任务逻辑身份
        :return NodeTask | None: 节点任务
        """
        return await self.session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))

    async def task_result(self, task_id: str) -> NodeTaskResult | None:
        """
        读取原任务最新结果以构造只读观察。

        :param task_id (str): 节点任务逻辑身份
        :return NodeTaskResult | None: 节点结果
        """
        return await self.session.scalar(
            select(NodeTaskResult)
            .where(NodeTaskResult.task_id == task_id)
            .order_by(NodeTaskResult.created_at.desc())
        )

    async def get_session(self, session_id: UUID) -> Session | None:
        """
        读取停止原始会话，删除后返回空值。

        :param session_id (UUID): 原会话身份
        :return Session | None: 当前会话或空值
        """
        return await self.session.get(Session, session_id)
