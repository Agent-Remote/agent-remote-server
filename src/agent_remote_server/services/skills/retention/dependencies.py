"""
统一枚举仍保留历史的精确内容义务，使预览闭包与实际退役使用同一依赖定义。
"""

from uuid import UUID

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.skill_manager.retention.dependencies import (
    MAX_HISTORY_EDGES,
    HistoryDependency,
)
from agent_remote_server.skill_manager.retention.graph import RetentionKey, RetentionKind
from agent_remote_server.skill_manager.retention.projection import RetentionProjection


def history_dependencies(
    index: RetentionIndex, projection: RetentionProjection | None = None
) -> tuple[HistoryDependency, ...]:
    """
    只让尚保留内容的消费者产生依赖，原 parent、回执和已退役比较不延长恢复义务。

    :param index (RetentionIndex): 完整已授权引用索引
    :param projection (RetentionProjection | None): 可选的整理后新增虚拟引用
    :return tuple[HistoryDependency, ...]: 完整有界的保留依赖集合
    """
    edges: set[HistoryDependency] = set()

    def add(
        kind: RetentionKind,
        identity: UUID,
        target_kind: RetentionKind,
        target: UUID | None,
        relation: str,
    ) -> None:
        """
        保留字段来源并去重，超过预算时整体拒绝不完整依赖分析。

        :param kind (RetentionKind): 消费者种类
        :param identity (UUID): 精确消费者身份
        :param target_kind (RetentionKind): 输入种类
        :param target (UUID | None): 可选精确输入身份
        :param relation (str): 持久化字段或成员关系
        """
        if target is None:
            return
        edges.add(
            HistoryDependency(
                RetentionKey(kind, str(identity)), RetentionKey(target_kind, str(target)), relation
            )
        )
        if len(edges) > MAX_HISTORY_EDGES:
            raise ValueError("history dependency edge limit exceeded")

    checkpoints = {row.id: row for row in index.checkpoints}
    for row in index.checkpoints:
        if row.retained:
            add("checkpoint", row.id, "checkpoint", row.backing_directory_id, "backing_directory")
    for member in index.members:
        if checkpoints[member.directory_checkpoint_id].retained:
            add(
                "checkpoint",
                member.directory_checkpoint_id,
                "checkpoint",
                member.checkpoint_id,
                "directory_member",
            )
    sources = {row.id: row for row in index.locals}
    for revision in index.local_revisions:
        if revision.retained and revision.number == 1:
            add(
                "local_revision",
                revision.id,
                "checkpoint",
                sources[revision.local_skill_id].source_checkpoint_id,
                "local_original",
            )
    snapshots = {row.id for row in index.snapshots if row.content_retired_at is None}
    for snapshot in index.snapshots:
        if snapshot.id in snapshots:
            add(
                "snapshot",
                snapshot.id,
                "checkpoint",
                snapshot.starting_checkpoint_id,
                "snapshot_directory",
            )
    for item in index.snapshot_items:
        if item.snapshot_id in snapshots:
            add("snapshot", item.snapshot_id, "checkpoint", item.checkpoint_id, "snapshot_item")
    for finalization in index.finalizations:
        if finalization.content_retired_at is None:
            add(
                "finalization",
                finalization.id,
                "snapshot",
                finalization.snapshot_id,
                "finalization_snapshot",
            )
            add(
                "finalization",
                finalization.id,
                "checkpoint",
                finalization.checkpoint_id,
                "finalization_checkpoint",
            )
    publications = {row.id for row in index.publications if row.content_retired_at is None}
    for publication in index.publications:
        if publication.id in publications:
            add(
                "publication",
                publication.id,
                "finalization",
                publication.finalization_id,
                "publication_finalization",
            )
            add(
                "publication",
                publication.id,
                "checkpoint",
                publication.expected_directory_id,
                "publication_expected_directory",
            )
            add(
                "publication",
                publication.id,
                "checkpoint",
                publication.result_checkpoint_id,
                "publication_result",
            )
    for branch in index.publication_branches:
        if branch.publication_id in publications:
            add(
                "publication",
                branch.publication_id,
                "checkpoint",
                branch.expected_checkpoint_id,
                "publication_branch",
            )
    for migration in index.migrations:
        if migration.content_retired_at is not None:
            continue
        for relation, checkpoint_id in (
            ("migration_base", migration.base_checkpoint_id),
            ("migration_current", migration.current_checkpoint_id),
            ("migration_source", migration.source_checkpoint_id),
            ("migration_directory", migration.directory_checkpoint_id),
            ("migration_result", migration.result_checkpoint_id),
            ("migration_result_directory", migration.result_directory_id),
        ):
            add("migration", migration.id, "checkpoint", checkpoint_id, relation)
    if projection is not None:
        for checkpoint in projection.checkpoints:
            add(
                "checkpoint",
                checkpoint.id,
                "checkpoint",
                checkpoint.backing_directory_id,
                "replacement_backing",
            )
        for projected_member in projection.members:
            add(
                "checkpoint",
                projected_member.directory_checkpoint_id,
                "checkpoint",
                projected_member.checkpoint_id,
                "replacement_member",
            )
    return tuple(sorted(edges))


def require_history_dependencies(
    dependencies: tuple[HistoryDependency, ...], selected: set[RetentionKey]
) -> None:
    """
    任何未同批退役的保留消费者都阻断输入退役，不依赖处理顺序或数据库回滚修复。

    :param dependencies (tuple[HistoryDependency, ...]): 完整保留历史依赖
    :param selected (set[RetentionKey]): 已通过保护与期限验证的实际退役身份
    """
    for edge in dependencies:
        if edge.dependency in selected and edge.consumer not in selected:
            raise SkillContentError(
                "HISTORY_REFERENCED", f"history has a retained {edge.consumer.kind} dependency"
            )
