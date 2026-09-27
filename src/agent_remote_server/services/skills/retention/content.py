"""
建立内容依赖与上传租约保护，逻辑额度分类不等于底层文件独占。
"""

from datetime import datetime

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.retention.uploads import (
    active_uploads,
    upload_file_references,
)
from agent_remote_server.skill_manager.retention.graph import (
    DirectoryReference,
    RetentionGraph,
    RetentionKind,
)
from agent_remote_server.skill_manager.retention.projection import RetentionProjection


def content(
    index: RetentionIndex,
    graph: RetentionGraph,
    now: datetime,
    projection: RetentionProjection | None = None,
) -> tuple[DirectoryReference, ...]:
    """
    树依赖显式对象引用；普通历史 parent 不传播保护，当前目录成员独立返回。

    :param index (RetentionIndex): 用户引用索引
    :param graph (RetentionGraph): 业务引用图
    :param now (datetime): 单次分析固定服务器时间
    :param projection (RetentionProjection | None): 新增假设内容边和当前目录替代身份
    :return tuple[DirectoryReference, ...]: 当前目录中退役前必须整理的成员
    """
    branch_map = {row.id: row for row in index.branches}
    for row in index.revisions:
        graph.edge("revision", row.id, "package_tree", row.tree_digest)
    for local_revision in index.local_revisions:
        graph.edge("local_revision", local_revision.id, "state_tree", local_revision.tree_digest)
    for checkpoint in index.checkpoints:
        graph.edge("checkpoint", checkpoint.id, "state_tree", checkpoint.tree_digest)
        graph.edge("checkpoint", checkpoint.id, "checkpoint", checkpoint.backing_directory_id)
        if checkpoint.state_id is not None:
            branch = branch_map[checkpoint.state_id]
            graph.edge("checkpoint", checkpoint.id, "revision", branch.base_revision_id)
            graph.edge("checkpoint", checkpoint.id, "local_revision", branch.local_revision_id)
    for ref in index.tree_objects:
        tree: RetentionKind = "package_tree" if ref.category == "package" else "state_tree"
        obj: RetentionKind = "package_object" if ref.category == "package" else "state_object"
        graph.edge(tree, ref.tree_digest, obj, ref.object_digest)
        graph.edge(obj, ref.object_digest, "blob", ref.object_digest)
    uploads = active_uploads(index, now)
    for upload in uploads.values():
        graph.root("upload", upload.id, "upload_lease")
    for upload_id, digest in upload_file_references(index, uploads):
        obj = "package_object" if uploads[upload_id].scope == "package" else "state_object"
        graph.edge("upload", upload_id, obj, digest)
        graph.edge(obj, digest, "blob", digest)
    for member in index.members:
        graph.edge(
            "directory_context",
            member.directory_checkpoint_id,
            "checkpoint",
            member.directory_checkpoint_id,
        )
        graph.edge(
            "directory_context", member.directory_checkpoint_id, "checkpoint", member.checkpoint_id
        )
    for checkpoint in index.checkpoints:
        if checkpoint.scope == "directory":
            graph.edge("directory_context", checkpoint.id, "checkpoint", checkpoint.id)
    replacements = dict(projection.directory_heads) if projection is not None else {}
    heads = {
        replacements.get(row.account_id, row.head_checkpoint_id)
        for row in index.directories
        if row.head_checkpoint_id is not None
    }
    projected_members = projection.members if projection is not None else ()
    if projection is not None:
        for projected in projection.checkpoints:
            graph.edge("checkpoint", projected.id, "state_tree", projected.tree_digest)
            graph.edge("checkpoint", projected.id, "checkpoint", projected.backing_directory_id)
            if projected.state_id is None:
                graph.edge("directory_context", projected.id, "checkpoint", projected.id)
            else:
                branch = branch_map[projected.state_id]
                graph.edge("checkpoint", projected.id, "revision", branch.base_revision_id)
                graph.edge("checkpoint", projected.id, "local_revision", branch.local_revision_id)
        for tree_digest, object_digest in projection.tree_objects:
            graph.edge("state_tree", tree_digest, "state_object", object_digest)
            graph.edge("state_object", object_digest, "blob", object_digest)
        for projected_member in projected_members:
            graph.edge(
                "directory_context",
                projected_member.directory_checkpoint_id,
                "checkpoint",
                projected_member.checkpoint_id,
            )
    for head in heads:
        assert head is not None
        graph.root("checkpoint", head, "current_directory")
    references = [
        DirectoryReference(
            account_id=row.account_id,
            directory_checkpoint_id=row.directory_checkpoint_id,
            entry_name=row.entry_name,
            state_id=row.state_id,
            checkpoint_id=row.checkpoint_id,
        )
        for row in index.members
        if row.directory_checkpoint_id in heads
    ]
    references.extend(row for row in projected_members if row.directory_checkpoint_id in heads)
    return tuple(sorted(references, key=lambda row: (row.account_id, row.entry_name)))
