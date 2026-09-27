"""
从规范范围一次分析完整候选，预先固定可执行组和仅启动等待的整理动作。
"""

from datetime import datetime

from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.repositories.skill_retention import (
    RetentionIndex,
    SkillRetentionRepository,
)
from agent_remote_server.services.skills.compaction.build import CompactionBuilder
from agent_remote_server.services.skills.compaction.projection import project_retention
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.gc.forecast import forecast_content
from agent_remote_server.services.skills.gc.planning import ContentReclamationPlan
from agent_remote_server.services.skills.prune.claims import claimed_content, source_key
from agent_remote_server.services.skills.prune.plan import PrunePlan, PruneScope
from agent_remote_server.services.skills.prune.scope import scoped_history
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.history import history_retention
from agent_remote_server.services.skills.retention.planning import (
    HistoryRetirementPlan,
    retirement_plan,
)
from agent_remote_server.services.skills.retention.trees import StoredTreeRetention
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.retention.groups import HistoryGroups, history_groups
from agent_remote_server.skill_manager.retention.projection import RetentionProjection
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def build_prune(
    index: RetentionIndex,
    scope: PruneScope,
    cutoff: datetime,
    generation: int,
    compactor: CompactionBuilder,
    repository: SkillRetentionRepository,
    content_repository: SkillContentGCRepository,
    policy: SkillStoragePolicy,
    *,
    all_unreferenced: bool,
) -> PrunePlan:
    """
    从完整阻断图传播资格再生成无阻断选择，执行前即可展示全部连带损失与保留原因。

    :param index (RetentionIndex): 已锁定完整所有者索引
    :param scope (PruneScope): 规范稳定来源范围
    :param cutoff (datetime): 原始固定截止
    :param generation (int): 用户库配置代数
    :param compactor (CompactionBuilder): 只读完整目录整理构建器
    :param repository (SkillRetentionRepository): 同事务历史仓储
    :param content_repository (SkillContentGCRepository): 同事务对象仓储
    :param policy (SkillStoragePolicy): 当前存储与等待策略
    :param all_unreferenced (bool): 是否明确提前结束等待
    :return PrunePlan: 全部候选、可执行组和精确整理/内容预测
    """
    directory = next((row for row in index.directories if row.account_id == scope.account_id), None)
    if directory is None or directory.mode != "managed_v1" or directory.head_checkpoint_id is None:
        raise SkillContentError("STATE_NOT_MANAGED", "account directory must be managed")
    requested = scoped_history(index, scope, cutoff)
    before = protection(index, cutoff)
    histories = history_retention(index, before, policy)
    eligible = {
        row.key
        for row in histories
        if not row.reasons
        and (all_unreferenced or (row.expires_at is not None and row.expires_at <= cutoff))
    } & set(requested)
    items = tuple(
        sorted(
            row.id
            for row in index.checkpoints
            if row.scope == "item"
            and row.retained
            and RetentionKey("checkpoint", str(row.id)) in eligible
        )
    )
    compaction = None
    overlay = RetentionProjection((), (), (), (), ())
    after = before
    if items:
        compaction = await compactor.build(
            index, scope.account_id, items, generation, all_unreferenced=all_unreferenced
        )
        projected = project_retention(index, compaction, cutoff, policy)
        overlay, before, after, histories = (
            projected.projection,
            projected.before,
            projected.after,
            projected.history,
        )
    active = await repository.active_migration_ids(
        scope.user_id, tuple(row.id for row in index.migrations), cutoff
    )
    candidates = None
    retirement = None
    selection = HistoryGroups((), (), ())
    trees: tuple[StoredTreeRetention, ...] = ()
    content = ContentReclamationPlan(scope.user_id, (), (), all_unreferenced, (), (), ())
    if requested:
        candidates = retirement_plan(
            index,
            scope.account_id,
            requested,
            histories,
            active,
            cutoff,
            all_unreferenced=all_unreferenced,
            projection=overlay,
        )
        selection = history_groups(
            candidates.requested,
            frozenset(row.key for row in candidates.entries if row.retained),
            candidates.dependencies,
            frozenset(row.key for row in candidates.entries if row.blockers),
        )
        if selection.roots:
            retirement = retirement_plan(
                index,
                scope.account_id,
                selection.roots,
                histories,
                active,
                cutoff,
                all_unreferenced=all_unreferenced,
                projection=overlay,
            )
            if not retirement.ready:
                raise ValueError("prune selected a blocked history group")
    extra_trees, extra_objects = await claimed_content(index, scope, content_repository)
    if retirement is not None or extra_trees or extra_objects:
        content_retirement = retirement or HistoryRetirementPlan(
            scope.user_id, scope.account_id, (), all_unreferenced, (), (), ()
        )
        trees, content = await forecast_content(
            index,
            content_retirement,
            overlay,
            before,
            after,
            cutoff,
            content_repository,
            policy,
            extra_trees=extra_trees,
            extra_objects=extra_objects,
        )
    claims = tuple(
        sorted(
            (row.id, row.source_key, row.kind, row.digest)
            for row in index.prune_claims
            if row.account_id == scope.account_id
            and (scope.source_id is None or row.source_key == source_key(scope))
        )
    )
    return PrunePlan(
        scope,
        cutoff,
        all_unreferenced,
        generation,
        compaction,
        overlay,
        before,
        after,
        histories,
        candidates,
        selection,
        retirement,
        trees,
        content,
        claims,
    )
