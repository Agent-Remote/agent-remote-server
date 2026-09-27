"""
未完成或可重试的配置部署必须固定精确版本，未知操作形状拒绝参与回收判断。
"""

from collections import defaultdict
from uuid import UUID

from agent_remote_server.models.skill_deployment import SkillDeploymentEntry, SkillDeploymentTarget
from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.schemas.skill_results import SkillMutationData
from agent_remote_server.services.skills.deployment_attempts import current_attempts
from agent_remote_server.services.skills.deployment_replacement import validate_replacement
from agent_remote_server.services.skills.deployment_validation import saved_plans
from agent_remote_server.services.skills.retention.deployment_discovery import discovered_plans
from agent_remote_server.skill_manager.retention.graph import RetentionGraph


def library_operations(index: RetentionIndex, graph: RetentionGraph) -> None:
    """
    普通终态回执不是永久根，待处理或可重试操作保留已固定版本。

    :param index (RetentionIndex): 单用户原始回执及版本集合
    :param graph (RetentionGraph): 保活图
    """
    revisions = {row.id: row for row in index.revisions}
    local_revisions = {row.id: row for row in index.local_revisions}
    pending = {"accepted", "preparing", "pending", "needs_resolution"}
    terminal = {"stored", "ready", "failed", "superseded", "partial_failure"}
    targets: dict[UUID, list[SkillDeploymentTarget]] = defaultdict(list)
    entries: dict[UUID, list[SkillDeploymentEntry]] = defaultdict(list)
    attempts: dict[UUID, list[SkillDeploymentAttempt]] = defaultdict(list)
    identities = {row.id for row in index.library_operations}
    operations = {row.id: row for row in index.library_operations}
    for attempt in index.deployment_attempts:
        if attempt.operation_id not in identities:
            raise ValueError("deployment attempt has no owned operation")
        attempts[attempt.operation_id].append(attempt)
    for target in index.deployment_targets:
        if target.operation_id not in identities:
            raise ValueError("deployment target has no owned operation")
        targets[target.operation_id].append(target)
    for entry in index.deployment_entries:
        if entry.operation_id not in identities:
            raise ValueError("deployment entry has no owned operation")
        entries[entry.operation_id].append(entry)
    for operation in index.library_operations:
        if operation.status not in pending | terminal:
            raise ValueError("unclassified skill operation retention state")
        data = SkillMutationData.model_validate(operation.result_json)
        plans = discovered_plans(
            index, saved_plans(operation, targets[operation.id], entries[operation.id])
        )
        current_attempts(operation, attempts[operation.id])
        if operation.replacement_id is not None and operation.attempts_version == 1:
            replacement = operations.get(operation.replacement_id)
            newer = (
                discovered_plans(
                    index,
                    saved_plans(replacement, targets[replacement.id], entries[replacement.id]),
                )
                if replacement is not None
                else ()
            )
            validate_replacement(operation, plans, replacement, newer)
        active_targets = any(
            target.readiness in {"pending", "needs_resolution"} for target in data.targets
        )
        if (operation.status == "superseded" and not active_targets) or (
            operation.status not in pending and not active_targets and not operation.retryable
        ):
            continue
        if not data.revision_ids and operation.plan_version is None:
            raise ValueError("pending skill operation has no fixed retention revisions")
        graph.root("operation", operation.id, "pending_operation")
        for revision in data.revision_ids:
            if revision in revisions:
                graph.edge("operation", operation.id, "revision", revision)
            elif revision in local_revisions:
                graph.edge("operation", operation.id, "local_revision", revision)
            else:
                raise ValueError("pending skill operation revision is outside owner index")
        for plan in plans:
            for source in plan.sources:
                if source.origin == "library":
                    package = revisions.get(source.revision_id)
                    if (
                        package is None
                        or package.installation_id != source.source_id
                        or package.content_digest != source.content_digest
                        or not package.retained
                        or package.tree_digest is None
                    ):
                        raise ValueError(
                            "deployment package reference is unavailable or mismatched"
                        )
                    graph.edge("operation", operation.id, "revision", source.revision_id)
                else:
                    local = local_revisions.get(source.revision_id)
                    if (
                        local is None
                        or local.local_skill_id != source.source_id
                        or local.account_id != plan.account_id
                        or local.content_digest != source.content_digest
                        or not local.retained
                        or local.tree_digest is None
                    ):
                        raise ValueError("deployment local reference is unavailable or mismatched")
                    graph.edge("operation", operation.id, "local_revision", source.revision_id)
