"""
从原计划和独立发现证据构建固定执行选择，不从当前账户规则修补历史。
"""

from collections.abc import Sequence
from uuid import UUID

from agent_remote_server.models.skill_deployment_discovery import (
    SkillDeploymentDiscoveredSource,
    SkillDeploymentDiscovery,
)
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.schemas.skill_deployment import DeploymentLocalSource, SkillDeploymentPlan


def resolved_plans(
    plans: Sequence[SkillDeploymentPlan],
    boundaries: Sequence[SkillDeploymentDiscovery],
    sources: Sequence[SkillDeploymentDiscoveredSource],
    takeovers: Sequence[SkillAccountTakeover],
) -> tuple[SkillDeploymentPlan, ...]:
    """
    核对补充来源库存、接管归属和两个摘要，原计划对象保持不变。

    :param plans (Sequence[SkillDeploymentPlan]): 已校验的原始计划
    :param boundaries (Sequence[SkillDeploymentDiscovery]): 同操作全部受理边界
    :param sources (Sequence[SkillDeploymentDiscoveredSource]): 同操作全部固定补充来源
    :param takeovers (Sequence[SkillAccountTakeover]): 原接管元数据
    :return tuple[SkillDeploymentPlan, ...]: 原始或已固定解析的执行计划
    """
    original = {(plan.user_id, plan.operation_id, plan.account_id): plan for plan in plans}
    indexed = {(row.user_id, row.operation_id, row.account_id): row for row in boundaries}
    if len(indexed) != len(boundaries) or not indexed.keys() <= original.keys():
        raise ValueError("deployment discovery target inventory is inconsistent")
    receipts = {row.id: row for row in takeovers}
    additions: dict[tuple[UUID, UUID, UUID], list[DeploymentLocalSource]] = {
        key: [] for key in indexed
    }
    for source in sources:
        key = (source.user_id, source.operation_id, source.account_id)
        if key not in indexed:
            raise ValueError("discovered source lacks original target boundary")
        additions[key].append(
            DeploymentLocalSource(
                source_id=source.source_id,
                revision_id=source.revision_id,
                content_digest=source.content_digest,
                name=source.name,
                enabled=True,
            )
        )
    for key, boundary in indexed.items():
        plan = original[key]
        extra = additions[key]
        if boundary.original_digest != plan.digest() or boundary.directory_epoch < 1:
            raise ValueError("deployment discovery changed accepted plan")
        if boundary.takeover_id is None:
            if extra or boundary.resolved_digest is not None:
                raise ValueError("unresolved discovery contains execution sources")
            continue
        receipt = receipts.get(boundary.takeover_id)
        if (
            receipt is None
            or receipt.status != "committed"
            or receipt.checkpoint_id is None
            or (
                receipt.user_id,
                receipt.account_id,
                receipt.node_id,
                receipt.runtime_backend,
                receipt.directory_epoch,
            )
            != (
                plan.user_id,
                plan.account_id,
                plan.node_id,
                plan.runtime_backend,
                boundary.directory_epoch,
            )
        ):
            raise ValueError("deployment discovery changed original takeover")
        known = {source.source_id for source in plan.sources if source.origin == "account_local"}
        if len({source.source_id for source in extra}) != len(extra) or known.intersection(
            source.source_id for source in extra
        ):
            raise ValueError("deployment discovery duplicates an accepted source")
        resolved = plan.model_copy(
            update={
                "sources": plan.sources
                + tuple(sorted(extra, key=lambda source: str(source.source_id)))
            }
        )
        if resolved.digest() != boundary.resolved_digest:
            raise ValueError("deployment discovery resolution digest mismatch")
        original[key] = resolved
    return tuple(original[(plan.user_id, plan.operation_id, plan.account_id)] for plan in plans)
