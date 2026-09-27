"""
将新配置与旧部署替代在同一保存点发布，保留独立执行观察及活动内容根。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.repositories.skill_deployment_lifecycle import (
    SkillDeploymentLifecycleRepository,
)
from agent_remote_server.services.skills.deployment_attempts import (
    current_attempts,
    save_projection,
)
from agent_remote_server.services.skills.deployment_discovery import execution_plans
from agent_remote_server.services.skills.deployment_replacement import (
    changed_accounts,
    validate_replacement,
)
from agent_remote_server.services.skills.deployment_validation import saved_plans


async def supersede_deployments(session: AsyncSession, replacement: SkillOperation) -> None:
    """
    只取代本次实际改变的未完成目标；替代标记不伪造取消或改写终态尝试。

    :param session (AsyncSession): 持有用户锁的新配置保存点
    :param replacement (SkillOperation): 已固定原计划和初始尝试的新受理
    """
    plans = SkillDeploymentRepository(session)
    attempts = SkillDeploymentAttemptRepository(session)
    targets, entries = await plans.rows(replacement.user_id, replacement.id)
    newer = await execution_plans(session, saved_plans(replacement, targets, entries))
    if not newer:
        return
    candidates = await SkillDeploymentLifecycleRepository(session).candidates(
        replacement, tuple(plan.account_id for plan in newer)
    )
    for original in candidates:
        targets, entries = await plans.rows(original.user_id, original.id)
        older = await execution_plans(session, saved_plans(original, targets, entries))
        current = current_attempts(original, await attempts.attempts(original.user_id, original.id))
        changed = changed_accounts(older, newer)
        if not any(
            current[account].status in {"pending", "running", "needs_resolution"}
            or current[account].retryable
            for account in changed
        ):
            continue
        original.replacement_id = replacement.id
        save_projection(original, current)
        validate_replacement(original, older, replacement, newer)
    await session.flush()
