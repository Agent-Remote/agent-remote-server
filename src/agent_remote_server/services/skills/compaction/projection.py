"""
把已重验的整理计划投影到保活图，保留旧身份并预计本次真正解除保护后的等待。
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.compaction.plan import CompactionPlan
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.history import (
    HistoryRetention,
    history_retention,
)
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.retention.graph import (
    DirectoryReference,
    RetentionProtection,
)
from agent_remote_server.skill_manager.retention.projection import (
    ProjectedCheckpoint,
    RetentionProjection,
)
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass(frozen=True)
class CompactionRetentionPreview:
    """
    预计保护与原始历史等待；虚拟身份不是持久化 checkpoint，也不构成删除授权。
    """

    plan: CompactionPlan
    analyzed_at: datetime
    projection: RetentionProjection
    before: RetentionProtection
    after: RetentionProtection
    history: tuple[HistoryRetention, ...]
    newly_released: tuple[HistoryRetention, ...]


def project_retention(
    index: RetentionIndex, plan: CompactionPlan, now: datetime, policy: SkillStoragePolicy
) -> CompactionRetentionPreview:
    """
    在同一固定时间比较新旧保护闭包，原始快照、冲突、基线和所有历史记录均不改写。

    :param index (RetentionIndex): 用户锁内的完整原始引用集合
    :param plan (CompactionPlan): 同一索引上已重新验证的完整整理计划
    :param now (datetime): 本次分析固定真实时间
    :param policy (SkillStoragePolicy): 当前部署的等待策略
    :return CompactionRetentionPreview: 只读假设结果，不包含退役或空间释放承诺
    """
    overlay = _projection(index, plan)
    before = protection(index, now)
    after = protection(index, now, overlay)
    history = []
    newly_released = []
    for original in history_retention(index, before, policy):
        reasons = after.protected.get(original.key, frozenset())
        released = original.released_at
        if reasons:
            released = None
        elif original.reasons:
            released = now
        days = policy.archive_days if original.archived else policy.history_days
        projected = replace(
            original,
            reasons=reasons,
            released_at=released,
            expires_at=released + timedelta(days=days) if released is not None else None,
        )
        history.append(projected)
        if original.reasons and not reasons:
            newly_released.append(projected)
    return CompactionRetentionPreview(
        plan=plan,
        analyzed_at=now,
        projection=overlay,
        before=before,
        after=after,
        history=tuple(history),
        newly_released=tuple(newly_released),
    )


def _projection(index: RetentionIndex, plan: CompactionPlan) -> RetentionProjection:
    """
    仅新增等价身份与结果树引用，旧成员和 backing 边仍由原索引保留。

    :param index (RetentionIndex): 原始已授权引用集合，用于拒绝虚拟身份碰撞
    :param plan (CompactionPlan): 已重验的内部整理计划
    :return RetentionProjection: 未持久化的图覆盖
    """
    if plan.user_id != index.user_id:
        raise ValueError("compaction projection owner mismatch")
    changed = plan.changed_directories()
    existing = {row.id for row in index.checkpoints}
    allocated: set[UUID] = set()

    def identity(kind: str, original: UUID, digest: str) -> UUID:
        """
        同一计划产生稳定虚拟身份，遇到真实身份或本次碰撞时整体拒绝分析。

        :param kind (str): 目录或单项命名空间
        :param original (UUID): 原始 checkpoint
        :param digest (str): 新的完整 backing 树摘要
        :return UUID: 仅分析期间有效的稳定虚拟身份
        """
        result = uuid5(plan.user_id, f"compaction-preview:{kind}:{original}:{digest}")
        if result in existing or result in allocated:
            raise ValueError("compaction projection identity collision")
        allocated.add(result)
        return result

    digests = {
        row.checkpoint_id: manifest_digest(row.result)
        for row in plan.directories
        if row.checkpoint_id in changed
    }
    directories = {
        original: identity("directory", original, digest) for original, digest in digests.items()
    }
    heads = {
        head.checkpoint_id: identity("item", head.checkpoint_id, digests[head.backing_directory_id])
        for head in plan.heads
        if head.backing_directory_id is not None and head.backing_directory_id in changed
    }
    checkpoints = [
        ProjectedCheckpoint(replacement, original, None, digests[original], None)
        for original, replacement in directories.items()
    ]
    for head in plan.heads:
        if head.checkpoint_id not in heads:
            continue
        backing = head.backing_directory_id
        assert backing is not None
        checkpoints.append(
            ProjectedCheckpoint(
                heads[head.checkpoint_id],
                head.checkpoint_id,
                head.state_id,
                digests[backing],
                directories[backing],
            )
        )
    members = []
    objects: set[tuple[str, str]] = set()
    for directory in plan.directories:
        if directory.checkpoint_id not in changed:
            continue
        removed = {row.checkpoint_id for row in directory.removed}
        for member in directory.members:
            if member.checkpoint_id not in removed:
                members.append(
                    DirectoryReference(
                        account_id=plan.account_id,
                        directory_checkpoint_id=directories[directory.checkpoint_id],
                        entry_name=member.entry_name,
                        state_id=member.state_id,
                        checkpoint_id=heads.get(member.checkpoint_id, member.checkpoint_id),
                    )
                )
        objects.update(
            (digests[directory.checkpoint_id], entry.sha256)
            for entry in directory.result.entries
            if entry.kind == "file"
        )
    return RetentionProjection(
        branch_heads=tuple(
            (head.state_id, heads[head.checkpoint_id])
            for head in plan.heads
            if head.checkpoint_id in heads
        ),
        directory_heads=((plan.account_id, directories[plan.directory_head_id]),)
        if plan.directory_head_id in changed
        else (),
        checkpoints=tuple(checkpoints),
        members=tuple(members),
        tree_objects=tuple(sorted(objects)),
    )
