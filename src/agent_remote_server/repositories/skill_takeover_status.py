"""
在一次只读查询中取得归属用户的原始接管和精确任务。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_takeover import SkillAccountTakeover


class SkillTakeoverStatusRepository:
    """
    以不可变接管所有者授权，不接受任务载荷声明的所有者。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定请求事务。

        :param session (AsyncSession): 数据库事务
        """
        self.session = session

    async def read(
        self, user_id: UUID, operation_id: UUID
    ) -> tuple[SkillAccountTakeover, NodeTask | None] | None:
        """
        原始任务缺失仍保留可诊断操作，但不能以此授权捕获。

        :param user_id (UUID): 认证用户身份
        :param operation_id (UUID): 原始接管身份
        :return tuple[SkillAccountTakeover, NodeTask | None] | None: 同一观察中的原始记录
        """
        row = (
            await self.session.execute(
                select(SkillAccountTakeover, NodeTask)
                .outerjoin(
                    NodeTask,
                    (NodeTask.id == SkillAccountTakeover.task_id)
                    & (NodeTask.node_id == SkillAccountTakeover.node_id),
                )
                .where(
                    SkillAccountTakeover.user_id == user_id,
                    SkillAccountTakeover.id == operation_id,
                )
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        return tuple(row) if row is not None else None
