"""
独立校验保存计划的完整性，拒绝用当前配置填补缺失输入。
"""

from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID

from agent_remote_server.models.skill_deployment import SkillDeploymentEntry, SkillDeploymentTarget
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.schemas.skill_deployment import (
    DeploymentLibrarySource,
    DeploymentLocalSource,
    DeploymentSelection,
    SkillDeploymentPlan,
)
from agent_remote_server.schemas.skill_results import SkillMutationData


def saved_plans(
    operation: SkillOperation,
    targets: Sequence[SkillDeploymentTarget],
    entries: Sequence[SkillDeploymentEntry],
) -> tuple[SkillDeploymentPlan, ...]:
    """
    对照原回执和规范摘要验证精确目标集合，历史无计划操作保持无计划。

    :param operation (SkillOperation): 已授权原始操作
    :param targets (Sequence[SkillDeploymentTarget]): 原始账户绑定行
    :param entries (Sequence[SkillDeploymentEntry]): 全部归属受约束的选择行
    :return tuple[SkillDeploymentPlan, ...]: 可供保活或未来重试使用的原始配置输入
    """
    data = SkillMutationData.model_validate(operation.result_json)
    if operation.plan_version is None:
        if targets or entries or any(target.plan_digest is not None for target in data.targets):
            raise ValueError("legacy operation has unexpected deployment plan rows")
        return ()
    if operation.plan_version != 1:
        raise ValueError("unclassified skill deployment plan version")
    if data.generation != operation.generation:
        raise ValueError("deployment generation differs from original receipt")
    expected = {target.account_id: target for target in data.targets}
    actual = {target.account_id: target for target in targets}
    if (
        len(expected) != len(data.targets)
        or len(actual) != len(targets)
        or expected.keys() != actual.keys()
    ):
        raise ValueError("deployment plan target inventory is incomplete")
    selections: dict[UUID, list[DeploymentSelection]] = defaultdict(list)
    identities: set[tuple[UUID, str, UUID]] = set()
    for entry in entries:
        identity = (entry.account_id, entry.origin, entry.source_id)
        if (
            entry.user_id != operation.user_id
            or entry.operation_id != operation.id
            or entry.account_id not in actual
            or identity in identities
        ):
            raise ValueError("deployment entry belongs to another target or is duplicated")
        identities.add(identity)
        selections[entry.account_id].append(_selection(entry))
    result = []
    for target in targets:
        receipt = expected[target.account_id]
        if (
            target.user_id != operation.user_id
            or target.operation_id != operation.id
            or target.node_id != receipt.node_id
            or target.plan_digest != receipt.plan_digest
        ):
            raise ValueError("deployment target differs from original receipt")
        plan = SkillDeploymentPlan(
            user_id=target.user_id,
            operation_id=target.operation_id,
            generation=operation.generation,
            account_id=target.account_id,
            node_id=target.node_id,
            tool_type=target.tool_type,
            runtime_backend=target.runtime_backend,
            sources=tuple(selections[target.account_id]),
        )
        if plan.digest() != target.plan_digest:
            raise ValueError("deployment plan digest mismatch")
        result.append(plan)
    return tuple(result)


def _selection(entry: SkillDeploymentEntry) -> DeploymentSelection:
    """
    只接受数据库两类明确来源，禁止空外键被解释为另一类来源。

    :param entry (SkillDeploymentEntry): 原始受约束选择行
    :return DeploymentSelection: 原始版本与启用选择
    """
    if (
        entry.origin == "library"
        and entry.installation_id == entry.source_id
        and entry.package_revision_id is not None
        and entry.installation_epoch is not None
        and entry.local_skill_id is None
        and entry.local_revision_id is None
    ):
        return DeploymentLibrarySource(
            source_id=entry.source_id,
            revision_id=entry.package_revision_id,
            content_digest=entry.content_digest,
            name=entry.name,
            enabled=entry.enabled,
            installation_epoch=entry.installation_epoch,
        )
    if (
        entry.origin == "account_local"
        and entry.local_skill_id == entry.source_id
        and entry.local_revision_id is not None
        and entry.installation_id is None
        and entry.installation_epoch is None
        and entry.package_revision_id is None
    ):
        return DeploymentLocalSource(
            source_id=entry.source_id,
            revision_id=entry.local_revision_id,
            content_digest=entry.content_digest,
            name=entry.name,
            enabled=entry.enabled,
        )
    raise ValueError("unclassified deployment source reference")
