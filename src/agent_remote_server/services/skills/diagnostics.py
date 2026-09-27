"""
在现有用户锁内组合真实配额和精确历史等待，不产生写入或文件访问。
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_diagnostics import SkillDiagnosticRepository
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_diagnostics import (
    SkillDeletionUsage,
    SkillHistoryDiagnostic,
    SkillStorageView,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.clocks import history_records
from agent_remote_server.services.skills.retention.history import history_retention
from agent_remote_server.services.skills.retention.planning import retained_history
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillDiagnosticService:
    """
    当前诊断独立于任何原始不可变受理回执。
    """

    def __init__(self, session: AsyncSession, policy: SkillStoragePolicy) -> None:
        """
        保留实际配置与请求仓储，不访问内容卷。

        :param session (AsyncSession): 当前用户事务
        :param policy (SkillStoragePolicy): 部署实际额度与等待期
        """
        self.storage = SkillStorageRepository(session)
        self.repository = SkillDiagnosticRepository(session)
        self.retention = SkillRetentionRepository(session)
        self.policy = policy

    async def storage_view(self, user_id: UUID) -> SkillStorageView:
        """
        空用户返回真实零用量，不为查询建立计量行。

        :param user_id (UUID): 认证所有者
        :return SkillStorageView: 同一用户锁内的逻辑和物理观察
        """
        usage = await self.storage.lock_existing_usage(user_id)
        deletion = await self.repository.deletion_usage(user_id)
        return SkillStorageView(
            observed_at=datetime.now(UTC),
            package_bytes=usage.package_bytes if usage else 0,
            state_bytes=usage.state_bytes if usage else 0,
            package_reserved_bytes=usage.package_reserved if usage else 0,
            state_reserved_bytes=usage.state_reserved if usage else 0,
            policy=self.policy,
            deletion=SkillDeletionUsage(
                pending_tasks=deletion.pending_tasks,
                retrying_tasks=deletion.retrying_tasks,
                completed_tasks=deletion.completed_tasks,
                pending_file_bytes=deletion.pending_file_bytes,
                cumulative_deleted_bytes=deletion.cumulative_deleted_bytes,
            ),
        )

    async def histories(
        self,
        user_id: UUID,
        kind: Literal["revision", "local_revision", "checkpoint"],
        identities: tuple[UUID, ...],
    ) -> dict[UUID, SkillHistoryDiagnostic]:
        """
        一次载入引用图，诊断精确已授权详情中的历史集合。

        :param user_id (UUID): 认证所有者
        :param kind (Literal["revision", "local_revision", "checkpoint"]): 明确历史种类
        :param identities (tuple[UUID, ...]): 需要解释的原始身份
        :return dict[UUID, SkillHistoryDiagnostic]: 每个请求身份的当前保留观察
        """
        if not identities:
            return {}
        await self.storage.lock_existing_usage(user_id)
        index = await self.retention.load(user_id)
        observed = datetime.now(UTC)
        records = history_records(index)
        clocks = {
            row.key: row
            for row in history_retention(index, protection(index, observed), self.policy)
        }
        result = {}
        for identity in identities:
            key = RetentionKey(kind, str(identity))
            if key not in records:
                raise SkillContentError("HISTORY_NOT_FOUND", "history not found")
            clock = clocks[key]
            retained = retained_history(records[key])
            state: Literal["protected", "waiting", "due", "release_unknown", "retired"]
            if not retained:
                state = "retired"
            elif clock.reasons:
                state = "protected"
            elif clock.expires_at is None:
                state = "release_unknown"
            elif clock.expires_at > observed:
                state = "waiting"
            else:
                state = "due"
            result[identity] = SkillHistoryDiagnostic(
                kind=kind,
                id=identity,
                observed_at=observed,
                retained=retained,
                state=state,
                protected_by=tuple(sorted(clock.reasons)),
                archived=clock.archived,
                retention_days=self.policy.archive_days
                if clock.archived
                else self.policy.history_days,
                released_at=clock.released_at,
                expires_at=clock.expires_at if retained else None,
            )
        return result
