"""
解释明确账户范围内仍存续的内容归属，不从墓碑摘要或创建时间推断权限。
"""

from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.prune.plan import PruneScope
from agent_remote_server.skill_manager.retention.graph import RetentionKey


def source_key(scope: PruneScope) -> str:
    """
    单项始终绑定稳定身份，完整目录具有独立原始选择标识。

    :param scope (PruneScope): 已授权规范范围
    :return str: 原归属来源键
    """
    return str(scope.source_id) if scope.source_id is not None else "*"


async def claimed_content(
    index: RetentionIndex, scope: PruneScope, repository: SkillContentGCRepository
) -> tuple[tuple[RetentionKey, ...], tuple[RetentionKey, ...]]:
    """
    账户可清理自身全部归属，单项只能延续同来源原资格；已标记对象不重复结算。

    :param index (RetentionIndex): 完整用户引用索引
    :param scope (PruneScope): 已授权规范范围
    :param repository (SkillContentGCRepository): 同事务对象仓储
    :return tuple[tuple[RetentionKey, ...], tuple[RetentionKey, ...]]: 明确树与仍可用状态对象
    """
    claims = [
        row
        for row in index.prune_claims
        if row.account_id == scope.account_id
        and (scope.source_id is None or row.source_key == source_key(scope))
    ]
    trees = {RetentionKey("state_tree", row.digest) for row in claims if row.kind == "tree"}
    digests = {row.digest for row in claims if row.kind == "object"}
    objects = {
        RetentionKey("state_object", row.digest)
        for row in await repository.objects(scope.user_id, digests)
        if row.category == "state" and row.status == "available"
    }
    return tuple(sorted(trees)), tuple(sorted(objects))
