"""
首次接管原子封存补充来源，独立执行计划不覆盖用户已接受的配置计划。
"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_deployment_discovery import SkillDeploymentDiscoveredSource
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.repositories.skill_deployment_discovery import (
    SkillDeploymentDiscoveryRepository,
)
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.schemas.skill_deployment import SkillDeploymentPlan
from agent_remote_server.services.skills.deployment_attempts import current_attempts
from agent_remote_server.services.skills.deployment_discovery_validation import resolved_plans
from agent_remote_server.services.skills.deployment_validation import saved_plans


async def execution_plans(
    session: AsyncSession, plans: Sequence[SkillDeploymentPlan]
) -> tuple[SkillDeploymentPlan, ...]:
    """
    只读取原操作已保存的发现证据，未解析边界不能推断后来的来源。

    :param session (AsyncSession): 已持有用户读锁或写锁的事务
    :param plans (Sequence[SkillDeploymentPlan]): 同操作原始完整计划
    :return tuple[SkillDeploymentPlan, ...]: 固定执行选择
    """
    if not plans:
        return ()
    repository = SkillDeploymentDiscoveryRepository(session)
    boundaries, sources = await repository.rows(plans[0].user_id, plans[0].operation_id)
    receipts = await repository.receipts(boundaries)
    return resolved_plans(plans, boundaries, sources, receipts)


async def resolve_takeover_discoveries(
    session: AsyncSession, receipt: SkillAccountTakeover
) -> None:
    """
    与首次接管提交同事务封存所有匹配原受理，不重读默认版本或启用规则。

    :param session (AsyncSession): 接管发布用户锁与保留保存点
    :param receipt (SkillAccountTakeover): 本事务刚提交的初始目录回执
    """
    if receipt.status != "committed" or receipt.checkpoint_id is None:
        raise ValueError("deployment discovery requires committed takeover")
    repository = SkillDeploymentDiscoveryRepository(session)
    boundaries = await repository.pending(receipt)
    if not boundaries:
        return
    additions = await repository.initial_sources(receipt)
    library = SkillLibraryRepository(session)
    for boundary in boundaries:
        operation = await library.operation(receipt.user_id, boundary.operation_id)
        if operation is None:
            raise ValueError("deployment discovery lost original operation")
        current = current_attempts(
            operation,
            await SkillDeploymentAttemptRepository(session).attempts(receipt.user_id, operation.id),
        )
        attempt = current.get(receipt.account_id)
        if attempt is None or (
            attempt.status not in {"pending", "running", "needs_resolution"}
            and not attempt.retryable
        ):
            continue
        plans = saved_plans(
            operation, *await SkillDeploymentRepository(session).rows(receipt.user_id, operation.id)
        )
        plan = next(plan for plan in plans if plan.account_id == receipt.account_id)
        resolved_plans((plan,), (boundary,), (), ())
        if (plan.node_id, plan.runtime_backend) != (receipt.node_id, receipt.runtime_backend):
            continue
        known = {source.source_id for source in plan.sources if source.origin == "account_local"}
        if known.intersection(source.source_id for source in additions):
            raise ValueError("takeover discovery overlaps accepted local sources")
        resolved = plan.model_copy(update={"sources": plan.sources + additions})
        boundary.takeover_id, boundary.resolved_digest = receipt.id, resolved.digest()
        for source in additions:
            session.add(
                SkillDeploymentDiscoveredSource(
                    user_id=plan.user_id,
                    operation_id=plan.operation_id,
                    account_id=plan.account_id,
                    source_id=source.source_id,
                    revision_id=source.revision_id,
                    name=source.name,
                    content_digest=source.content_digest,
                )
            )
    await session.flush()
