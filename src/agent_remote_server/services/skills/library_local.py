"""
构建账户本地来源的只读详情，不初始化或修改运行状态。
"""

from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.schemas.skill_results import SkillLocalRevisionView, SkillLocalView
from agent_remote_server.schemas.skill_rules import ResolvedSkillRule


async def local_view(repository: SkillLocalRepository, item: AccountLocalSkill) -> SkillLocalView:
    """
    将本地身份及保留历史投影为独立来源视图。

    :param repository (SkillLocalRepository): 当前事务的本地仓储
    :param item (AccountLocalSkill): 已授权发布来源
    :return SkillLocalView: 不宣称部署就绪的配置详情
    """
    assert item.default_revision_id is not None
    assert item.status in {"active", "removed"}
    return SkillLocalView(
        id=item.id,
        account_id=item.account_id,
        name=item.name,
        status="active" if item.status == "active" else "removed",
        enabled=item.enabled,
        default_revision_id=item.default_revision_id,
        source_checkpoint_id=item.source_checkpoint_id,
        revisions=[
            SkillLocalRevisionView(
                id=row.id,
                number=row.number,
                content_digest=row.content_digest,
                retained=row.retained,
                subtree_prefix=row.subtree_prefix,
                metadata=row.metadata_json,
            )
            for row in await repository.revisions(item)
        ],
        effective=ResolvedSkillRule(
            enabled=item.enabled,
            revision_id=item.default_revision_id,
            enabled_source="account",
            revision_source="account",
            eligible=item.status == "active",
            included=item.enabled and item.status == "active",
            exclusion_reason="removed" if item.status == "removed" else None,
        ),
    )
