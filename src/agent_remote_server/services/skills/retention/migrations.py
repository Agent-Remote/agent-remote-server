"""
区分完整未解决比较与仍在使用的精确成功增量基线。
"""

from uuid import UUID

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.skill_manager.retention.graph import RetentionGraph


def migrations(index: RetentionIndex, graph: RetentionGraph) -> None:
    """
    冲突保留全部三侧；最新成功只让对应目标依赖确切来源 checkpoint。

    :param index (RetentionIndex): 当前用户一致引用集合
    :param graph (RetentionGraph): 保护图
    """
    branches = {row.id: row for row in index.branches}
    directories = {row.account_id: row for row in index.directories}
    latest: dict[tuple[UUID, UUID], SkillBranchPreparation] = {}
    for row in index.migrations:
        if row.status == "conflicted":
            graph.root("migration", row.id, "migration_conflict")
        if row.content_retired_at is None:
            graph.edge("migration", row.id, "branch", row.source_state_id)
            graph.edge("migration", row.id, "branch", row.target_state_id)
            for checkpoint in (
                row.base_checkpoint_id,
                row.current_checkpoint_id,
                row.source_checkpoint_id,
                row.directory_checkpoint_id,
                row.result_checkpoint_id,
                row.result_directory_id,
            ):
                graph.edge("migration", row.id, "checkpoint", checkpoint)
            for directory_id in (row.directory_checkpoint_id, row.result_directory_id):
                graph.edge("migration", row.id, "directory_context", directory_id)
            for digest in (row.base_digest, row.current_digest, row.incoming_digest):
                graph.edge("migration", row.id, "state_tree", digest)
        if row.status != "ready" or row.migration_sequence is None or row.source_state_id is None:
            continue
        source = branches[row.source_state_id]
        target = branches[row.target_state_id]
        directory = directories[row.account_id]
        if (source.epoch, target.epoch, directory.epoch) != (
            row.source_epoch,
            row.target_epoch,
            row.directory_epoch,
        ):
            continue
        pair = (source.id, target.id)
        old = latest.get(pair)
        if old is None or (old.migration_sequence or 0) < row.migration_sequence:
            latest[pair] = row
    for row in latest.values():
        graph.edge("branch", row.target_state_id, "migration_baseline", row.id)
        graph.edge("migration_baseline", row.id, "checkpoint", row.source_checkpoint_id)
    for custom in index.migration_content:
        if custom.content_retired_at is not None:
            continue
        graph.edge("migration", custom.migration_id, "state_tree", custom.tree_digest)
