"""
活动部署和可重试输入保护完整目录，结束后的任务绑定仅保留历史身份。
"""

from collections import defaultdict
from uuid import UUID

from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.deployment_attempts import current_attempts
from agent_remote_server.services.skills.retention.deployment_discovery import discovery_roots
from agent_remote_server.skill_manager.retention.graph import RetentionGraph


def deployments(index: RetentionIndex, graph: RetentionGraph) -> None:
    """
    配置替代不等于执行排空，原活动任务的完整输入仍必须保活。

    :param index (RetentionIndex): 单用户完整一致索引
    :param graph (RetentionGraph): 业务引用图
    """
    discovery_roots(index, graph)
    attempts: dict[UUID, list[SkillDeploymentAttempt]] = defaultdict(list)
    identities = {row.id: row for row in index.deployment_attempts}
    if not index.deployment_tasks:
        return
    for row in index.deployment_attempts:
        attempts[row.operation_id].append(row)
    operations = {row.id: row for row in index.library_operations}
    current = {row.id: current_attempts(row, attempts[row.id]) for row in index.library_operations}
    checkpoints = {row.id: row for row in index.checkpoints}
    inputs: dict[tuple[UUID, UUID], UUID] = {}
    tasks = {row.id: row for row in index.deployment_node_tasks}
    targets = {(row.operation_id, row.account_id): row for row in index.deployment_targets}
    resolutions = {
        (row.operation_id, row.account_id): row.resolved_digest
        for row in index.deployment_discoveries
    }
    active = {"pending", "running", "needs_resolution"}
    for binding in index.deployment_tasks:
        attempt = identities.get(binding.attempt_id)
        task = tasks.get(binding.task_id)
        checkpoint = checkpoints.get(binding.checkpoint_id)
        target = targets.get((binding.operation_id, binding.account_id))
        if (
            task is None
            or task.node_id != binding.node_id
            or task.task_type != "prepare_account_skills"
            or task.task_id != f"prepare_account_skills:{binding.attempt_id}"
            or task.status
            not in {"pending", "leased", "running", "succeeded", "failed", "cancelled", "expired"}
            or target is None
            or target.node_id != binding.node_id
            or (resolutions.get((binding.operation_id, binding.account_id)) or target.plan_digest)
            != binding.plan_digest
            or attempt is None
            or attempt.user_id != binding.user_id
            or attempt.operation_id != binding.operation_id
            or attempt.account_id != binding.account_id
            or attempt.plan_digest != target.plan_digest
            or checkpoint is None
            or checkpoint.account_id != binding.account_id
            or checkpoint.scope != "directory"
            or checkpoint.content_digest != binding.content_digest
        ):
            raise ValueError("deployment task input has inconsistent ownership")
        key = (binding.operation_id, binding.account_id)
        if inputs.setdefault(key, binding.checkpoint_id) != binding.checkpoint_id:
            raise ValueError("deployment retries changed their original input")
        operation = operations[binding.operation_id]
        latest = current[binding.operation_id][binding.account_id]
        retained = (
            task.status in {"pending", "leased", "running", "expired"}
            or attempt.status in active
            or latest.status in active
            or latest.retryable
            and operation.replacement_id is None
            and operation.status != "superseded"
        )
        if retained:
            graph.root("directory_context", binding.checkpoint_id, "pending_operation")
