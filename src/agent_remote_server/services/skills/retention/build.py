"""
组合明确业务根，保留目录整理义务并返回完整闭包。
"""

from datetime import datetime
from uuid import UUID

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.retention.branches import branches
from agent_remote_server.services.skills.retention.content import content
from agent_remote_server.services.skills.retention.deployments import deployments
from agent_remote_server.services.skills.retention.library import library_operations
from agent_remote_server.services.skills.retention.migrations import migrations
from agent_remote_server.services.skills.retention.operations import operations
from agent_remote_server.skill_manager.retention.graph import (
    RetentionGraph,
    RetentionKind,
    RetentionProtection,
)
from agent_remote_server.skill_manager.retention.projection import RetentionProjection


def protection(
    index: RetentionIndex, now: datetime, projection: RetentionProjection | None = None
) -> RetentionProtection:
    """
    不把缺少硬保护误当成到期或删除授权。

    :param index (RetentionIndex): 已授权且完整的单用户引用索引
    :param now (datetime): 固定分析时间，用于上传租约判断
    :param projection (RetentionProjection | None): 已重验整理计划的只读替代边
    :return RetentionProtection: 保活闭包及尚待整理的目录成员引用
    """
    if now.tzinfo is None:
        raise ValueError("retention analysis requires an aware timestamp")
    graph = RetentionGraph()
    branches(index, graph, projection)
    library_operations(index, graph)
    deployments(index, graph)
    members = content(index, graph, now, projection)
    operations(index, graph)
    migrations(index, graph)
    result = graph.protect(members)
    retired: tuple[tuple[RetentionKind, tuple[UUID, ...]], ...] = (
        ("checkpoint", tuple(row.id for row in index.checkpoints if not row.retained)),
        (
            "snapshot",
            tuple(row.id for row in index.snapshots if row.content_retired_at is not None),
        ),
        (
            "finalization",
            tuple(row.id for row in index.finalizations if row.content_retired_at is not None),
        ),
        (
            "publication",
            tuple(row.id for row in index.publications if row.content_retired_at is not None),
        ),
        (
            "migration",
            tuple(row.id for row in index.migrations if row.content_retired_at is not None),
        ),
    )
    for kind, identities in retired:
        if any(result.reasons(kind, identity) for identity in identities):
            raise SkillContentError("STATE_EXPIRED", "active reference targets retired history")
    return result
