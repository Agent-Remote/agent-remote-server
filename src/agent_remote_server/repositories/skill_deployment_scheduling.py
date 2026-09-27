"""
有界筛选原节点尚未绑定任务的最新尝试，不从新账户位置重新选择目标。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from agent_remote_server.models.skill_deployment import SkillDeploymentTarget
from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask


@dataclass(frozen=True)
class DeploymentCandidate:
    """
    只携带原身份，锁外候选不构成执行权限。
    """

    user_id: UUID
    operation_id: UUID
    account_id: UUID
    attempt_id: UUID


async def deployment_candidates(
    session: AsyncSession, node_id: UUID, limit: int
) -> tuple[DeploymentCandidate, ...]:
    """
    按最后检查时间轮转，排除历史尝试及任何已有部署任务的尝试。

    :param session (AsyncSession): 本次轮询的元数据事务
    :param node_id (UUID): 认证原节点
    :param limit (int): 本次有界候选数
    :return tuple[DeploymentCandidate, ...]: 等待用户锁内重新授权的身份
    """
    attempt = SkillDeploymentAttempt
    target = SkillDeploymentTarget
    successor = aliased(SkillDeploymentAttempt)
    rows = await session.execute(
        select(attempt.user_id, attempt.operation_id, attempt.account_id, attempt.id)
        .join(
            target,
            (target.user_id == attempt.user_id)
            & (target.operation_id == attempt.operation_id)
            & (target.account_id == attempt.account_id),
        )
        .where(
            target.node_id == node_id,
            attempt.status.in_(("pending", "needs_resolution")),
            ~select(successor.id)
            .where(
                successor.user_id == attempt.user_id,
                successor.operation_id == attempt.operation_id,
                successor.account_id == attempt.account_id,
                successor.predecessor_id == attempt.id,
            )
            .exists(),
            ~select(SkillDeploymentTask.attempt_id)
            .where(SkillDeploymentTask.attempt_id == attempt.id)
            .exists(),
        )
        .order_by(attempt.updated_at, attempt.id)
        .limit(limit)
    )
    return tuple(DeploymentCandidate(*row) for row in rows)
