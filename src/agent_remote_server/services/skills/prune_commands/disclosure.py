"""
按稳定顺序完整披露候选、依赖和整理，不把大清单嵌入确认请求。
"""

from collections.abc import Iterator
from uuid import UUID

from agent_remote_server.schemas.skill_prune import PruneBinding, PruneSummary
from agent_remote_server.schemas.skill_prune_rows import (
    PruneCompactionRow,
    PruneDependencyRow,
    PruneDisclosure,
    PruneHistoryRow,
    PruneIdentity,
    PruneMemberRow,
)
from agent_remote_server.schemas.skill_state_commands import SkillStateSelector
from agent_remote_server.services.skills.prune.plan import PrunePlan, PruneResult
from agent_remote_server.services.skills.prune_commands.digest import digest
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.retention.graph import RetentionKey


def identity(key: RetentionKey) -> PruneIdentity:
    """
    历史披露只接受真实或预计的 UUID 身份。

    :param key (RetentionKey): 历史图身份
    :return PruneIdentity: 明确种类和原身份
    """
    return PruneIdentity(kind=key.kind, id=UUID(key.identity))


def summary(plan: PrunePlan) -> PruneSummary:
    """
    从同一完整计划生成规范范围和精确动作数量。

    :param plan (PrunePlan): 完整已授权内部计划
    :return PruneSummary: 所有页及受理回执共用的摘要
    """
    compact = plan.compaction
    changed = compact.changed_directories() if compact is not None else frozenset()
    return PruneSummary(
        binding=PruneBinding(
            selector=SkillStateSelector(
                account_id=plan.scope.account_id,
                scope="item" if plan.scope.source_id is not None else "account-directory",
                skill=str(plan.scope.source_id) if plan.scope.source_id is not None else None,
            ),
            cutoff=plan.cutoff,
            all_unreferenced=plan.all_unreferenced,
            plan_digest=digest(plan),
        ),
        ready=plan.content.ready,
        history_losses=sum(len(group) for group in plan.selection.groups),
        groups=len(plan.selection.groups),
        blocked_histories=len(plan.selection.blocked),
        compacted_directories=len(changed),
        compacted_items=sum(head.backing_directory_id in changed for head in compact.heads)
        if compact
        else 0,
        trees=len(plan.content.requested_trees),
        package_bytes=plan.content.package_bytes,
        state_bytes=plan.content.state_bytes,
        pending_file_bytes=plan.content.pending_file_bytes,
    )


def disclosures(plan: PrunePlan, result: PruneResult | None = None) -> Iterator[PruneDisclosure]:
    """
    披露整份候选及全部依赖，实际结果仅补充替换身份，不重算原损失。

    :param plan (PrunePlan): 用户审阅的完整原计划
    :param result (PruneResult | None): 同事务实际执行结果，预览为空
    :return Iterator[PruneDisclosure]: 稳定连续且无截断的完整披露
    """
    groups = {key: number for number, group in enumerate(plan.selection.groups, 1) for key in group}
    blocked = set(plan.selection.blocked)
    if plan.candidates is not None:
        for row in plan.candidates.entries:
            retention = row.retention
            yield PruneHistoryRow(
                history=identity(row.key),
                retained=row.retained,
                selected=row.key in groups,
                group=groups.get(row.key),
                blockers=row.blockers,
                dependency_blocked=row.key in blocked,
                protected_by=tuple(sorted(retention.reasons)) if retention else (),
                released_at=retention.released_at if retention else None,
                expires_at=retention.expires_at if retention else None,
                archived=retention.archived if retention else False,
                content_digests=row.content_digests,
            )
        for edge in plan.candidates.dependencies:
            yield PruneDependencyRow(
                consumer=identity(edge.consumer),
                dependency=identity(edge.dependency),
                relation=edge.relation,
            )
    compact = plan.compaction
    if compact is None:
        return
    actual = result.compaction if result is not None else None
    directories = dict(actual.directory_replacements) if actual else {}
    heads = dict(actual.head_replacements) if actual else {}
    changed = compact.changed_directories()
    digests = {row.checkpoint_id: manifest_digest(row.result) for row in compact.directories}
    for directory in compact.directories:
        if directory.checkpoint_id in changed:
            yield PruneCompactionRow(
                scope="directory",
                checkpoint_id=directory.checkpoint_id,
                state_id=None,
                epoch=directory.directory_epoch,
                original_digest=directory.tree_digest,
                result_digest=digests[directory.checkpoint_id],
                replacement_id=directories.get(directory.checkpoint_id),
            )
        for member in directory.removed:
            yield PruneMemberRow(
                directory_id=directory.checkpoint_id,
                checkpoint_id=member.checkpoint_id,
                state_id=member.state_id,
                name=member.entry_name,
                action="removed",
            )
        for member in directory.blocked:
            yield PruneMemberRow(
                directory_id=directory.checkpoint_id,
                checkpoint_id=member.checkpoint_id,
                state_id=member.state_id,
                name=member.entry_name,
                action="blocked",
            )
    for head in compact.heads:
        if head.backing_directory_id in changed:
            assert head.backing_directory_id is not None
            yield PruneCompactionRow(
                scope="item",
                checkpoint_id=head.checkpoint_id,
                state_id=head.state_id,
                epoch=head.state_epoch,
                original_digest=head.tree_digest,
                result_digest=digests[head.backing_directory_id],
                replacement_id=heads.get(head.checkpoint_id),
            )
