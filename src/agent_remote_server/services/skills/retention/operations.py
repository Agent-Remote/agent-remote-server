"""
保活未结束会话、待发布输入和未解决冲突，不让终态操作永久占用内容。
"""

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.skill_manager.retention.graph import RetentionGraph


def operations(index: RetentionIndex, graph: RetentionGraph) -> None:
    """
    活动流程保留完整比较上下文；终态记录仅建立依赖边，不自行成为根。

    :param index (RetentionIndex): 所有者限定的引用集合
    :param graph (RetentionGraph): 待补充的语义依赖图
    """
    sessions = {row.id: row for row in index.sessions}
    for snapshot in index.snapshots:
        session = sessions.get(snapshot.session_id) if snapshot.session_id is not None else None
        if snapshot.status in {"reserved", "started", "finalizing"} or (
            session is not None and session.status not in {"stopped", "interrupted", "failed"}
        ):
            graph.root("snapshot", snapshot.id, "active_snapshot")
        if snapshot.content_retired_at is not None:
            continue
        graph.edge("snapshot", snapshot.id, "directory_context", snapshot.starting_checkpoint_id)
        graph.edge("snapshot", snapshot.id, "state_tree", snapshot.tree_digest)
    for member in index.snapshot_items:
        graph.edge("snapshot", member.snapshot_id, "branch", member.state_id)
        graph.edge("snapshot", member.snapshot_id, "checkpoint", member.checkpoint_id)
    for finalization in index.finalizations:
        if finalization.status in {"upload_pending", "persisted", "persisted_unclean"}:
            graph.root("finalization", finalization.id, "pending_finalization")
        if finalization.content_retired_at is not None:
            continue
        graph.edge("finalization", finalization.id, "snapshot", finalization.snapshot_id)
        graph.edge("finalization", finalization.id, "directory_context", finalization.checkpoint_id)
        graph.edge("finalization", finalization.id, "state_tree", finalization.tree_digest)
    for publication in index.publications:
        if publication.status == "conflicted":
            graph.root("publication", publication.id, "publication_conflict")
        if publication.content_retired_at is not None:
            continue
        graph.edge("publication", publication.id, "finalization", publication.finalization_id)
        graph.edge(
            "publication", publication.id, "directory_context", publication.expected_directory_id
        )
        graph.edge(
            "publication", publication.id, "directory_context", publication.result_checkpoint_id
        )
        graph.edge("publication", publication.id, "state_tree", publication.current_tree_digest)
    for branch in index.publication_branches:
        graph.edge("publication", branch.publication_id, "branch", branch.state_id)
        graph.edge(
            "publication", branch.publication_id, "checkpoint", branch.expected_checkpoint_id
        )
    for choice in index.choices:
        if choice.content_retired_at is not None:
            continue
        graph.edge("publication", choice.publication_id, "state_tree", choice.tree_digest)
    for takeover in index.takeovers:
        if takeover.status != "committed":
            graph.root("takeover", takeover.id, "pending_takeover")
        graph.edge("takeover", takeover.id, "upload", takeover.upload_id)
        graph.edge("takeover", takeover.id, "directory_context", takeover.checkpoint_id)
        graph.edge("takeover", takeover.id, "state_tree", takeover.capture_digest)
