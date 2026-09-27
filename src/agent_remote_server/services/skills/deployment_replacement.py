"""
仅凭两份原始计划核对替代关系，不将代数变化或运行完成当作计划变化。
"""

from collections.abc import Sequence
from uuid import UUID

from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.schemas.skill_deployment import SkillDeploymentPlan
from agent_remote_server.schemas.skill_results import SkillMutationData


def changed_accounts(
    original: Sequence[SkillDeploymentPlan], replacement: Sequence[SkillDeploymentPlan]
) -> frozenset[UUID]:
    """
    忽略新受理身份和代数，保留内容、启用值、纪元及节点后端的精确比较。

    :param original (Sequence[SkillDeploymentPlan]): 原操作固定计划
    :param replacement (Sequence[SkillDeploymentPlan]): 新操作固定计划
    :return frozenset[UUID]: 两个操作共同目标中实际改变的账户
    """
    newer = {plan.account_id: plan for plan in replacement}
    changed = set()
    for old in original:
        candidate = newer.get(old.account_id)
        if candidate is None:
            continue
        comparable = candidate.model_copy(
            update={"operation_id": old.operation_id, "generation": old.generation}
        )
        if comparable.digest() != old.digest():
            changed.add(old.account_id)
    return frozenset(changed)


def validate_replacement(
    original: SkillOperation,
    original_plans: Sequence[SkillDeploymentPlan],
    replacement: SkillOperation | None,
    replacement_plans: Sequence[SkillDeploymentPlan],
) -> None:
    """
    拒绝缺失、跨用户、回退、循环或不相关的替代证据，不能据此释放内容。

    :param original (SkillOperation): 已保存的原操作
    :param original_plans (Sequence[SkillDeploymentPlan]): 校验后的原计划
    :param replacement (SkillOperation | None): 按原所有者读取的替代受理
    :param replacement_plans (Sequence[SkillDeploymentPlan]): 校验后的替代计划
    """
    if original.replacement_id is None or original.attempts_version is None:
        return
    if (
        replacement is None
        or original.replacement_id != replacement.id
        or original.user_id != replacement.user_id
        or replacement.generation <= original.generation
        or not replacement.committed
        or replacement.plan_version != 1
        or replacement.attempts_version != 1
        or not SkillMutationData.model_validate(replacement.result_json).changed
        or original.status != "superseded"
        or original.retryable
        or not changed_accounts(original_plans, replacement_plans)
    ):
        raise ValueError("deployment replacement evidence is inconsistent")
