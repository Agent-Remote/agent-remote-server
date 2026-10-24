"""
在原会话行锁之后读取关联停止任务和不可变原结果。
"""

from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask, NodeTaskResult, Session


class SessionDeletionRepository:
    """
    保留全部停止尝试，不能用较新成功覆盖另一条未解决的原任务。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        复用历史删除事务。

        :param session (AsyncSession): 已锁定会话的外层事务
        """
        self.session = session

    async def stop_tasks(self, runtime: Session) -> Sequence[NodeTask]:
        """
        按完整关联寻找任务，损坏的规范任务指针不能绕过检查。

        :param runtime (Session): 已锁定原会话
        :return Sequence[NodeTask]: 锁定并刷新的全部停止任务
        """
        return (
            await self.session.scalars(
                select(NodeTask)
                .where(
                    or_(
                        NodeTask.task_id.in_(
                            (
                                f"stop_tool_session:{runtime.id}",
                                f"cleanup_tool_session:{runtime.id}",
                            )
                        ),
                        (NodeTask.task_type == "stop_tool_session")
                        & (NodeTask.payload["session_id"].as_string() == str(runtime.id)),
                    )
                )
                .order_by(NodeTask.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()

    async def receipts(self, task: NodeTask) -> Sequence[NodeTaskResult]:
        """
        同时检查逻辑键与原行键，拒绝错绑或重复收据。

        :param task (NodeTask): 锁定的原停止任务
        :return Sequence[NodeTaskResult]: 原逻辑键或行键关联的全部收据
        """
        return (
            await self.session.scalars(
                select(NodeTaskResult)
                .where(
                    or_(
                        NodeTaskResult.task_id == task.task_id,
                        NodeTaskResult.node_task_id == task.id,
                    )
                )
                .execution_options(populate_existing=True)
            )
        ).all()
