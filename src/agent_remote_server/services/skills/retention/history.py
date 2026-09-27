"""
解释普通与卸载归档历史的等待时间，截止时间本身不授予删除资格。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.retention.clocks import history_records
from agent_remote_server.skill_manager.retention.graph import (
    ProtectionReason,
    RetentionKey,
    RetentionKind,
    RetentionProtection,
)
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass(frozen=True)
class HistoryRetention:
    """
    真实历史身份的只读等待信息，未知释放证据与保护理由分别保留。
    """

    key: RetentionKey
    reasons: frozenset[ProtectionReason]
    archived: bool
    released_at: datetime | None
    expires_at: datetime | None


def history_retention(
    index: RetentionIndex, protected: RetentionProtection, policy: SkillStoragePolicy
) -> tuple[HistoryRetention, ...]:
    """
    使用当前部署天数计算 UTC 截止，不填补、推进或清空数据库时钟。

    :param index (RetentionIndex): 完整所有者索引
    :param protected (RetentionProtection): 同事务同时间的有效保护闭包
    :param policy (SkillStoragePolicy): 当前发布配置的保留天数
    :return tuple[HistoryRetention, ...]: 稳定身份排序的历史等待信息
    """
    archived = _archived_identities(index)
    result = []
    for key, row in sorted(history_records(index).items()):
        released = row.retention_released_at
        if released is not None:
            released = released.replace(tzinfo=UTC) if released.tzinfo is None else released
        reasons = protected.protected.get(key, frozenset())
        days = policy.archive_days if key in archived else policy.history_days
        result.append(
            HistoryRetention(
                key=key,
                reasons=reasons,
                archived=key in archived,
                released_at=released,
                expires_at=released + timedelta(days=days)
                if released is not None and not reasons
                else None,
            )
        )
    return tuple(result)


def _archived_identities(index: RetentionIndex) -> set[RetentionKey]:
    """
    完整上下文含归档成员时采用归档期，不把重新安装的新纪元等同于旧分支。

    :param index (RetentionIndex): 已授权全部真实身份
    :return set[RetentionKey]: 应使用卸载归档等待期的历史身份
    """
    installations = {row.id: row for row in index.installations}
    local = {row.id: row for row in index.locals}
    branches = {
        row.id
        for row in index.branches
        if (
            row.installation_id is not None
            and (
                installations[row.installation_id].removed
                or installations[row.installation_id].epoch != row.installation_epoch
            )
        )
        or (row.local_skill_id is not None and local[row.local_skill_id].status == "removed")
    }
    directories = {row.directory_checkpoint_id for row in index.members if row.state_id in branches}
    snapshots = {row.snapshot_id for row in index.snapshot_items if row.state_id in branches}
    snapshots.update(row.id for row in index.snapshots if row.starting_checkpoint_id in directories)
    archived_sessions = {row.session_reference_id for row in index.snapshots if row.id in snapshots}
    finalizations = {row.id for row in index.finalizations if row.snapshot_id in snapshots}
    checkpoints = {
        row.id
        for row in index.checkpoints
        if row.state_id in branches
        or row.id in directories
        or row.source_session_reference_id in archived_sessions
    }
    groups: tuple[tuple[RetentionKind, set[UUID]], ...] = (
        (
            "revision",
            {row.id for row in index.revisions if installations[row.installation_id].removed},
        ),
        (
            "local_revision",
            {
                row.id
                for row in index.local_revisions
                if local[row.local_skill_id].status == "removed"
            },
        ),
        ("checkpoint", checkpoints),
        ("snapshot", snapshots),
        ("finalization", finalizations),
        (
            "publication",
            {row.id for row in index.publications if row.finalization_id in finalizations},
        ),
        (
            "migration",
            {
                row.id
                for row in index.migrations
                if row.source_state_id in branches or row.target_state_id in branches
            },
        ),
    )
    return {
        RetentionKey(kind, str(identity)) for kind, identities in groups for identity in identities
    }
