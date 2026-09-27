"""
只读推演完整历史退役后的实际树外键、替代引用和树等待，不改写任何原 ORM 行。
"""

from dataclasses import replace
from datetime import datetime, timedelta

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.retention.planning import HistoryRetirementPlan
from agent_remote_server.services.skills.retention.trees import (
    StoredTreeReference,
    StoredTreeRetention,
    tree_retention,
)
from agent_remote_server.skill_manager.retention.graph import RetentionKey, RetentionProtection
from agent_remote_server.skill_manager.retention.projection import RetentionProjection
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


def projected_content_trees(
    index: RetentionIndex,
    retirement: HistoryRetirementPlan,
    projection: RetentionProjection,
    before: RetentionProtection,
    after: RetentionProtection,
    now: datetime,
    policy: SkillStoragePolicy,
    *,
    extra_trees: tuple[RetentionKey, ...] = (),
) -> tuple[StoredTreeRetention, ...]:
    """
    仅分析确实解除过本次历史外键的状态树，其他账户或未绑定上传树不被自动选择。

    :param index (RetentionIndex): 当前完整所有者索引
    :param retirement (HistoryRetirementPlan): 已无阻断的全部历史退役计划
    :param projection (RetentionProjection): 完整等价目录及单项引用覆盖
    :param before (RetentionProtection): 原保护闭包
    :param after (RetentionProtection): 整理后保护闭包
    :param now (datetime): 固定分析时刻，新解除保护从此预计等待
    :param policy (SkillStoragePolicy): 当前部署保留策略
    :param extra_trees (tuple[RetentionKey, ...]): 持久化生命周期归属授权的续扫树
    :return tuple[StoredTreeRetention, ...]: 本次历史退役影响的全部状态树及剩余阻断
    """
    if retirement.user_id != index.user_id or not retirement.ready:
        raise ValueError("content projection requires an authorized complete retirement plan")
    selected = {row.key for row in retirement.entries if row.retained}
    if any(
        key.kind not in {"checkpoint", "snapshot", "finalization", "publication", "migration"}
        for key in selected
    ):
        raise ValueError("content projection cannot retire original versions")
    claimed = set(extra_trees)
    if any(key.kind != "state_tree" for key in claimed):
        raise ValueError("content projection claims must reference state trees")
    replacements: dict[RetentionKey, set[StoredTreeReference]] = {}
    for checkpoint in projection.checkpoints:
        key = RetentionKey("state_tree", checkpoint.tree_digest)
        replacements.setdefault(key, set()).add(
            StoredTreeReference(
                "skill_checkpoints",
                (str(checkpoint.id),),
                "tree_digest",
                retirement.account_id,
            )
        )
    result = []
    for original in tree_retention(index, before, policy):
        removed = {ref for ref in original.references if ref.history_key in selected}
        if not removed and original.key not in claimed:
            continue
        if original.key.kind != "state_tree" or any(
            ref.account_id != retirement.account_id for ref in removed
        ):
            raise ValueError("content projection cannot expand account scope")
        references = (set(original.references) - removed) | replacements.get(original.key, set())
        reasons = after.protected.get(original.key, frozenset())
        released = original.released_at
        if reasons:
            released = None
        elif original.reasons:
            released = now
        result.append(
            replace(
                original,
                references=tuple(sorted(references)),
                reasons=reasons,
                released_at=released,
                expires_at=released + timedelta(days=policy.history_days)
                if released is not None and not reasons
                else None,
            )
        )
    return tuple(sorted(result, key=lambda row: row.key))
