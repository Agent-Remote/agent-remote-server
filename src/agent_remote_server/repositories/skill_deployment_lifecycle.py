"""
在用户锁内筛选与新计划共享目标的既有未完成操作。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_deployment import SkillDeploymentTarget
from agent_remote_server.models.skill_library import SkillOperation


class SkillDeploymentLifecycleRepository:
    """
    只提供同用户旧操作候选，不以当前账户绑定重建历史计划。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        复用持有用户锁的配置事务。

        :param session (AsyncSession): 调用方保存点
        """
        self.session = session

    async def candidates(
        self, replacement: SkillOperation, accounts: tuple[UUID, ...]
    ) -> tuple[SkillOperation, ...]:
        """
        已有替代、完成操作及未知历史不会被后来的代数重新解释。

        :param replacement (SkillOperation): 已固定计划的新受理
        :param accounts (tuple[UUID, ...]): 本次实际受影响账户
        :return tuple[SkillOperation, ...]: 待比较的原始操作
        """
        target = SkillDeploymentTarget
        operation = SkillOperation
        return tuple(
            await self.session.scalars(
                select(operation)
                .where(
                    operation.user_id == replacement.user_id,
                    operation.generation < replacement.generation,
                    operation.replacement_id.is_(None),
                    operation.plan_version == 1,
                    operation.attempts_version == 1,
                    operation.status.in_(
                        (
                            "accepted",
                            "preparing",
                            "pending",
                            "needs_resolution",
                            "failed",
                            "partial_failure",
                        )
                    ),
                    select(target.operation_id)
                    .where(
                        target.user_id == replacement.user_id,
                        target.operation_id == operation.id,
                        target.account_id.in_(accounts),
                    )
                    .exists(),
                )
                .order_by(operation.generation, operation.id)
                .execution_options(populate_existing=True)
            )
        )
