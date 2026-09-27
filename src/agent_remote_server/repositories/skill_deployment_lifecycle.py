"""
在用户锁内筛选与新计划共享目标的既有未完成操作。
"""

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from agent_remote_server.models.skill_deployment import SkillDeploymentEntry, SkillDeploymentTarget
from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
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

    async def unfinished_accounts(self, user_id: UUID, sources: tuple[UUID, ...]) -> set[UUID]:
        """
        保留受影响来源尚未完成的精确目标，禁用后改版本仍可取代旧计划。

        :param user_id (UUID): 已锁定所有者
        :param sources (tuple[UUID, ...]): 本次变更的来源身份
        :return set[UUID]: 当前仍活动或可重试且包含原来源的账户
        """
        attempt = SkillDeploymentAttempt
        successor = aliased(attempt)
        entry = SkillDeploymentEntry
        return set(
            await self.session.scalars(
                select(attempt.account_id)
                .join(SkillOperation, SkillOperation.id == attempt.operation_id)
                .where(
                    attempt.user_id == user_id,
                    SkillOperation.user_id == user_id,
                    SkillOperation.replacement_id.is_(None),
                    or_(
                        attempt.status.in_(("pending", "running", "needs_resolution")),
                        attempt.retryable.is_(True),
                    ),
                    ~select(successor.id).where(successor.predecessor_id == attempt.id).exists(),
                    select(entry.source_id)
                    .where(
                        entry.user_id == user_id,
                        entry.operation_id == attempt.operation_id,
                        entry.account_id == attempt.account_id,
                        entry.source_id.in_(sources),
                    )
                    .exists(),
                )
                .distinct()
            )
        )

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
