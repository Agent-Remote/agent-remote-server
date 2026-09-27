"""
已接受发现边界保护原接管输入，解析来源沿原操作状态保留而非永久保活。
"""

from collections.abc import Sequence

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.schemas.skill_deployment import SkillDeploymentPlan
from agent_remote_server.services.skills.deployment_discovery_validation import resolved_plans
from agent_remote_server.skill_manager.retention.graph import RetentionGraph


def discovered_plans(
    index: RetentionIndex, plans: Sequence[SkillDeploymentPlan]
) -> tuple[SkillDeploymentPlan, ...]:
    """
    单用户完整索引内复用执行计划验证，并检查新增版本的精确初始来源。

    :param index (RetentionIndex): 当前用户一致元数据
    :param plans (Sequence[SkillDeploymentPlan]): 同操作原始计划
    :return tuple[SkillDeploymentPlan, ...]: 已验证固定执行计划
    """
    if not plans:
        return ()
    operation_id = plans[0].operation_id
    boundaries = tuple(
        row for row in index.deployment_discoveries if row.operation_id == operation_id
    )
    sources = tuple(
        row for row in index.deployment_discovered_sources if row.operation_id == operation_id
    )
    result = resolved_plans(plans, boundaries, sources, index.takeovers)
    local = {row.id: row for row in index.locals}
    revisions = {row.id: row for row in index.local_revisions}
    takeovers = {row.id: row for row in index.takeovers}
    by_account = {row.account_id: row for row in boundaries}
    for source in sources:
        boundary = by_account[source.account_id]
        receipt = takeovers.get(boundary.takeover_id) if boundary.takeover_id is not None else None
        item = local.get(source.source_id)
        revision = revisions.get(source.revision_id)
        if (
            receipt is None
            or item is None
            or revision is None
            or (
                item.user_id,
                item.account_id,
                item.source_checkpoint_id,
                item.name,
                revision.local_skill_id,
                revision.account_id,
                revision.number,
                revision.content_digest,
                revision.subtree_prefix,
            )
            != (
                source.user_id,
                source.account_id,
                receipt.checkpoint_id,
                source.name,
                source.source_id,
                source.account_id,
                1,
                source.content_digest,
                source.name,
            )
        ):
            raise ValueError("discovered source differs from original takeover metadata")
    return result


def discovery_roots(index: RetentionIndex, graph: RetentionGraph) -> None:
    """
    即使部署任务尚未建立，活动受理也保留唯一初始目录以供原解析或重试。

    :param index (RetentionIndex): 当前用户完整索引
    :param graph (RetentionGraph): 原操作根决定是否可达的引用图
    """
    targets = {(row.operation_id, row.account_id): row for row in index.deployment_targets}
    for boundary in index.deployment_discoveries:
        target = targets.get((boundary.operation_id, boundary.account_id))
        if target is None or target.user_id != boundary.user_id:
            raise ValueError("discovery boundary lacks original target")
        for receipt in index.takeovers:
            if (
                receipt.user_id,
                receipt.account_id,
                receipt.node_id,
                receipt.runtime_backend,
                receipt.directory_epoch,
            ) == (
                boundary.user_id,
                boundary.account_id,
                target.node_id,
                target.runtime_backend,
                boundary.directory_epoch,
            ):
                graph.edge("operation", boundary.operation_id, "takeover", receipt.id)
