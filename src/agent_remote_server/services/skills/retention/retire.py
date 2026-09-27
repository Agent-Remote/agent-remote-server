"""
按完整保护、截止时间和历史依赖原子退役 checkpoint 与比较内容，不删除审计身份或文件。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_retention import (
    SkillRetentionRepository,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.clocks import history_records, retention_mutation
from agent_remote_server.services.skills.retention.dependencies import (
    history_dependencies,
    require_history_dependencies,
)
from agent_remote_server.services.skills.retention.history import history_retention
from agent_remote_server.skill_manager.retention.dependencies import MAX_RETIREMENT_IDENTITIES
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy

type RetirableHistory = (
    SessionSkillSnapshot
    | SkillFinalization
    | SkillPublication
    | SkillBranchPreparation
    | SkillCheckpoint
)


class SkillHistoryRetirementService:
    """
    内部账户范围入口，公开 prune 仍须另外完成目录整理、精确请求和物理 GC。
    """

    def __init__(self, session: AsyncSession, policy: SkillStoragePolicy) -> None:
        """
        外层调用方保留最终提交权，全部身份在同一用户锁内复核。

        :param session (AsyncSession): 已授权调用方事务
        :param policy (SkillStoragePolicy): 当前历史等待配置
        """
        self._session = session
        self._repository = SkillRetentionRepository(session)
        self._policy = policy

    async def retire(
        self,
        user_id: UUID,
        account_id: UUID,
        keys: tuple[RetentionKey, ...],
        *,
        all_unreferenced: bool = False,
    ) -> tuple[RetentionKey, ...]:
        """
        先验证整份精确选择，再同时退役历史与所属人工授权，重试跳过已完成身份。

        :param user_id (UUID): 已认证所有者
        :param account_id (UUID): 明确目标账户
        :param keys (tuple[RetentionKey, ...]): 已选定的完整历史身份集合
        :param all_unreferenced (bool): 是否明确提前结束历史等待，不能绕过有效保护
        :return tuple[RetentionKey, ...]: 本次实际退役身份，非配额结算或物理删除回执
        """
        if not keys or len(keys) > MAX_RETIREMENT_IDENTITIES or len(set(keys)) != len(keys):
            raise SkillContentError(
                "INVALID_REQUEST",
                "select distinct history identities within the retention index limit",
            )
        async with retention_mutation(self._session, user_id):
            index = await self._repository.load(user_id)
            now = datetime.now(UTC)
            protected = protection(index, now)
            histories = {row.key: row for row in history_retention(index, protected, self._policy)}
            records = history_records(index)
            selected: dict[RetentionKey, RetirableHistory] = {}
            for key in keys:
                record = records.get(key)
                if (
                    not isinstance(
                        record,
                        (
                            SessionSkillSnapshot,
                            SkillFinalization,
                            SkillPublication,
                            SkillBranchPreparation,
                            SkillCheckpoint,
                        ),
                    )
                    or record.account_id != account_id
                ):
                    raise SkillContentError(
                        "HISTORY_NOT_FOUND", "history not found in this account"
                    )
                if (
                    not record.retained
                    if isinstance(record, SkillCheckpoint)
                    else record.content_retired_at is not None
                ):
                    continue
                view = histories[key]
                if view.reasons:
                    raise SkillContentError("STATE_PROTECTED", "history has active protection")
                if not all_unreferenced and (view.expires_at is None or view.expires_at > now):
                    raise SkillContentError(
                        "HISTORY_NOT_EXPIRED", "history waiting period has not expired"
                    )
                selected[key] = record
            require_history_dependencies(history_dependencies(index), set(selected))
            migration_ids = tuple(
                record.id
                for record in selected.values()
                if isinstance(record, SkillBranchPreparation)
            )
            if await self._repository.has_active_migration_upload(user_id, migration_ids, now):
                raise SkillContentError(
                    "STATE_PROTECTED", "history still has an active input upload"
                )
            for record in selected.values():
                if isinstance(record, SkillCheckpoint):
                    record.retained = False
                    record.tree_digest = None
                else:
                    record.content_retired_at = now
            for branch in index.branches:
                if RetentionKey("checkpoint", str(branch.head_checkpoint_id)) in selected:
                    branch.expired = True
            for choice in index.choices:
                if RetentionKey("publication", str(choice.publication_id)) in selected:
                    choice.content_retired_at = now
            for grant in index.migration_content:
                if RetentionKey("migration", str(grant.migration_id)) in selected:
                    grant.content_retired_at = now
            await self._session.flush()
            return tuple(sorted(selected))
