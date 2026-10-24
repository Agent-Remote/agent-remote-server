"""
为原会话任务终态结算提供同事务行锁和刷新后的收据读取。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask, NodeTaskResult, Session


class SessionTaskResultRepository:
    """
    按会话再任务的顺序串行结算，避免使用等待锁之前的陈旧对象。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        复用结果处理的外层事务。

        :param session (AsyncSession): 原请求事务
        """
        self.session = session

    async def lock_task(
        self, session_id: UUID, record_id: UUID, node_id: UUID, task_id: str, kind: str
    ) -> NodeTask | None:
        """
        依次锁定原会话与完整身份匹配的任务行。

        :param session_id (UUID): 原会话身份，允许展示行已经删除
        :param record_id (UUID): 原任务数据库行身份
        :param node_id (UUID): 已认证节点身份
        :param task_id (str): 原逻辑任务身份
        :param kind (str): 原任务类型
        :return NodeTask | None: 刷新后的原任务或不存在
        """
        await self.session.scalar(
            select(Session)
            .where(Session.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(
            select(NodeTask)
            .where(
                NodeTask.id == record_id,
                NodeTask.node_id == node_id,
                NodeTask.task_id == task_id,
                NodeTask.task_type == kind,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def receipt(self, task_id: str) -> NodeTaskResult | None:
        """
        原任务已锁定后读取最新持久收据。

        :param task_id (str): 原逻辑任务身份
        :return NodeTaskResult | None: 已提交的终态收据或不存在
        """
        return await self.session.scalar(
            select(NodeTaskResult)
            .where(NodeTaskResult.task_id == task_id)
            .execution_options(populate_existing=True)
        )
