"""
复用真实结算规则预测精确历史退役后的内容，不依赖一定存在目录整理。
"""

from datetime import datetime

from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.gc.build import affected_objects, reclamation_plan
from agent_remote_server.services.skills.gc.planning import ContentReclamationPlan
from agent_remote_server.services.skills.gc.projection import projected_content_trees
from agent_remote_server.services.skills.retention.planning import HistoryRetirementPlan
from agent_remote_server.services.skills.retention.trees import StoredTreeRetention
from agent_remote_server.skill_manager.retention.graph import RetentionKey, RetentionProtection
from agent_remote_server.skill_manager.retention.projection import RetentionProjection
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def forecast_content(
    index: RetentionIndex,
    retirement: HistoryRetirementPlan,
    projection: RetentionProjection,
    before: RetentionProtection,
    after: RetentionProtection,
    now: datetime,
    repository: SkillContentGCRepository,
    policy: SkillStoragePolicy,
    *,
    extra_trees: tuple[RetentionKey, ...] = (),
    extra_objects: tuple[RetentionKey, ...] = (),
) -> tuple[tuple[StoredTreeRetention, ...], ContentReclamationPlan]:
    """
    只从无阻断历史选择推演受影响树，新增对象边与两类别有效租约参与同一结算。

    :param index (RetentionIndex): 同锁完整所有者索引
    :param retirement (HistoryRetirementPlan): 完整无阻断的精确历史退役
    :param projection (RetentionProjection): 可能为空的等价视图覆盖
    :param before (RetentionProtection): 原保护闭包
    :param after (RetentionProtection): 整理后保护闭包
    :param now (datetime): 固定分析时刻
    :param repository (SkillContentGCRepository): 同事务对象仓储
    :param policy (SkillStoragePolicy): 当前等待策略
    :param extra_trees (tuple[RetentionKey, ...]): 原生命周期归属证明的待清理树
    :param extra_objects (tuple[RetentionKey, ...]): 因原租约暂留且归属仍有效的状态对象
    :return tuple[tuple[StoredTreeRetention, ...], ContentReclamationPlan]: 全部受影响树与准确结算
    """
    trees = projected_content_trees(
        index,
        retirement,
        projection,
        before,
        after,
        now,
        policy,
        extra_trees=extra_trees,
    )
    selected = tuple(
        tree.key
        for tree in trees
        if not tree.reasons
        and not tree.references
        and (
            retirement.all_unreferenced or (tree.expires_at is not None and tree.expires_at <= now)
        )
    )
    objects = affected_objects(index, selected) | set(extra_objects)
    rows = await repository.objects(index.user_id, {key.identity for key in objects})
    content = reclamation_plan(
        index.user_id,
        index,
        trees,
        rows,
        selected,
        extra_objects,
        now,
        all_unreferenced=retirement.all_unreferenced,
        projected_tree_objects=projection.tree_objects,
    )
    return trees, content
