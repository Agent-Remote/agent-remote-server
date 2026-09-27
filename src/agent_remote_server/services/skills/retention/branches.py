"""
按版本解析和保留 pin 确定当前分支，停用不解除学习数据保护。
"""

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.skill_manager.retention.graph import RetentionGraph
from agent_remote_server.skill_manager.retention.projection import RetentionProjection


def branches(
    index: RetentionIndex, graph: RetentionGraph, projection: RetentionProjection | None = None
) -> None:
    """
    规则只影响同来源当前安装纪元，不把归档 head 或使用顺序永久保活。

    :param index (RetentionIndex): 单用户一致引用集合
    :param graph (RetentionGraph): 待补充根与边的图
    :param projection (RetentionProjection | None): 仅覆盖当前 head 的只读假设
    """
    heads = dict(projection.branch_heads) if projection is not None else {}
    active = {row.id: row for row in index.installations if not row.removed}
    accounts = {row.id: row for row in index.accounts}
    tools = {(row.installation_id, row.tool_type): row.revision_id for row in index.tools}
    overrides = {(row.installation_id, row.account_id): row.revision_id for row in index.overrides}
    for library in active.values():
        if library.default_revision_id is not None:
            graph.root("revision", library.default_revision_id, "library_default")
    pins = [row.revision_id for row in index.tools] + [row.revision_id for row in index.overrides]
    for revision in pins:
        if revision is not None:
            graph.root("revision", revision, "pin")
    local = {row.id: row for row in index.locals if row.status == "active"}
    for source in local.values():
        if source.default_revision_id is not None:
            graph.root("local_revision", source.default_revision_id, "local_original")
        graph.root("directory_context", source.source_checkpoint_id, "local_original")
    for branch in index.branches:
        if not branch.expired:
            graph.edge(
                "branch", branch.id, "checkpoint", heads.get(branch.id, branch.head_checkpoint_id)
            )
        graph.edge("branch", branch.id, "revision", branch.base_revision_id)
        graph.edge("branch", branch.id, "local_revision", branch.local_revision_id)
        if branch.local_skill_id in local:
            source = local[branch.local_skill_id]
            if branch.local_revision_id == source.default_revision_id:
                graph.root("branch", branch.id, "current_branch")
        if branch.installation_id not in active:
            continue
        item = active[branch.installation_id]
        account = accounts[branch.account_id]
        if branch.installation_epoch != item.epoch:
            continue
        tool = tools.get((item.id, account.tool_type))
        override = overrides.get((item.id, account.id))
        selected = override or tool or item.default_revision_id
        if branch.base_revision_id == selected:
            graph.root("branch", branch.id, "current_branch")
        if branch.base_revision_id in {pin for pin in (tool, override) if pin is not None}:
            graph.root("branch", branch.id, "pin")
