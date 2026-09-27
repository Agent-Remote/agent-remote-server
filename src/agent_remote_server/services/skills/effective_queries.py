"""
只读解释当前账户状态及原会话固定选择，不创建分支或调用准备流程。
"""

from datetime import UTC
from typing import Literal, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_effective import SkillEffectiveRepository
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_effective import (
    AccountSkillView,
    SessionSkillItemView,
    SessionSkillView,
)
from agent_remote_server.schemas.skill_results import SkillInstallationView, SkillLocalView
from agent_remote_server.schemas.skill_rules import ResolvedSkillRule
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.system_views import saved_systems


class SkillEffectiveQueryService:
    """
    用户锁覆盖读取，原会话和当前账户的身份来源明确分离。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        只组合读取仓储，不构造内容上传或物化服务。

        :param session (AsyncSession): 当前用户事务
        """
        self.repository = SkillEffectiveRepository(session)
        self.runtime = SkillRuntimeRepository(session)
        self.storage = SkillStorageRepository(session)
        self.local = SkillLocalRepository(session)

    async def account(
        self, user_id: UUID, account_id: UUID, item: SkillInstallationView | SkillLocalView
    ) -> AccountSkillView:
        """
        调用方已通过当前账户和来源授权，固定该规则选中的精确分支。

        :param user_id (UUID): 认证用户
        :param account_id (UUID): 已授权账户
        :param item (SkillInstallationView | SkillLocalView): 已授权且包含账户规则的来源
        :return AccountSkillView: 不触发迁移的当前状态解释
        """
        await self.storage.lock_existing_usage(user_id)
        rule = item.effective
        assert rule is not None
        if isinstance(item, SkillLocalView):
            branch = await self.local.branch(user_id, account_id, item.id, rule.revision_id)
            previous = await self.local.has_previous_branch(user_id, account_id, item.id)
        else:
            branch = await self.runtime.branch(
                user_id, account_id, item.id, item.epoch, rule.revision_id
            )
            previous = await self.runtime.has_previous_branch(
                user_id, account_id, item.id, item.epoch
            )
        directory = await self.runtime.directory(user_id, account_id)
        (
            publication_count,
            publication_id,
            migration_count,
            migration_id,
        ) = await self.repository.conflicts(user_id, account_id, item.id)
        synced, unknown = await self.repository.sync_times(user_id, account_id, item.id)
        if synced is not None and synced.tzinfo is None:
            synced = synced.replace(tzinfo=UTC)
        preparation: Literal["initialized", "uninitialized", "migration_required", "state_expired"]
        if branch is not None and branch.expired:
            preparation = "state_expired"
        elif branch is not None and branch.head_checkpoint_id is not None:
            preparation = "initialized"
        else:
            preparation = "migration_required" if previous else "uninitialized"
        return AccountSkillView(
            account_id=account_id,
            revision_selection_reason=(
                "account_local_revision"
                if isinstance(item, SkillLocalView)
                else "account_pin"
                if rule.revision_source == "account"
                else "tool_pin"
                if rule.revision_source == "tool"
                else "user_default"
            ),
            directory_mode=cast(Literal["legacy", "migrating", "managed_v1"], directory.mode)
            if directory
            else "legacy",
            directory_epoch=directory.epoch if directory else None,
            directory_checkpoint_id=directory.head_checkpoint_id if directory else None,
            state_id=branch.id if branch else None,
            state_epoch=branch.epoch if branch else None,
            checkpoint_id=branch.head_checkpoint_id if branch else None,
            state_expired=branch.expired if branch else False,
            preparation=preparation,
            publication_conflicts=publication_count,
            migration_conflicts=migration_count,
            latest_publication_conflict_id=publication_id,
            latest_migration_conflict_id=migration_id,
            last_recorded_sync_at=synced,
            unknown_sync_times=unknown,
        )

    async def session(
        self, user_id: UUID, session_id: UUID, limit: int = 100, cursor: str | None = None
    ) -> SessionSkillView:
        """
        原快照在内容退役或会话删除后仍按原用户授权，不按当前选择重建。

        :param user_id (UUID): 认证用户
        :param session_id (UUID): 原始会话身份
        :param limit (int): 单页成员数量
        :param cursor (str | None): 同快照上一页末尾名称
        :return SessionSkillView: 原始选择或明确未记录的旧会话
        """
        if not 1 <= limit <= 200 or (cursor is not None and not 1 <= len(cursor) <= 64):
            raise SkillContentError("INVALID_REQUEST", "invalid snapshot page")
        await self.storage.lock_existing_usage(user_id)
        snapshot = await self.runtime.snapshot_for_session(user_id, session_id)
        if snapshot is None:
            original = await self.runtime.session(user_id, session_id)
            if original is None:
                raise SkillContentError("SESSION_NOT_FOUND", "session not found")
            if cursor is not None:
                raise SkillContentError("INVALID_REQUEST", "legacy session has no snapshot cursor")
            return SessionSkillView(
                session_id=session_id,
                account_id=original.tool_account_id,
                basis="legacy_unrecorded",
                snapshot_id=None,
                snapshot_status=None,
                content_retained=None,
                runtime_backend=cast(Literal["native", "docker_sandbox"], original.runtime_backend),
                library_generation=None,
                directory_epoch=None,
                starting_checkpoint_id=None,
                tree_digest=None,
                system_items=[],
                items=[],
                next_cursor=None,
            )
        if cursor is not None and not await self.repository.cursor_exists(snapshot, cursor):
            raise SkillContentError("INVALID_REQUEST", "cursor is not a member of this snapshot")
        rows = await self.repository.members(snapshot, limit + 1, cursor)
        items = []
        for member, branch, checkpoint in rows[:limit]:
            source = branch.installation_id or branch.local_skill_id
            revision = branch.base_revision_id or branch.local_revision_id
            assert source is not None and revision is not None
            resolution = ResolvedSkillRule.model_validate(member.resolution_json)
            if resolution.revision_id != revision or not resolution.included:
                raise SkillContentError(
                    "SNAPSHOT_METADATA_INVALID", "saved resolution does not match its branch"
                )
            items.append(
                SessionSkillItemView(
                    name=member.entry_name,
                    skill_id=source,
                    origin="user_library" if branch.installation_id else "account_local",
                    revision_id=revision,
                    installation_epoch=branch.installation_epoch,
                    state_id=member.state_id,
                    state_epoch=member.state_epoch,
                    checkpoint_id=member.checkpoint_id,
                    checkpoint_retained=checkpoint.retained,
                    resolution=resolution,
                )
            )
        return SessionSkillView(
            session_id=session_id,
            account_id=snapshot.account_id,
            basis="session_snapshot",
            snapshot_id=snapshot.id,
            snapshot_status=snapshot.status,
            content_retained=snapshot.content_retired_at is None,
            runtime_backend=cast(Literal["native", "docker_sandbox"], snapshot.runtime_backend),
            library_generation=snapshot.library_generation,
            directory_epoch=snapshot.directory_epoch,
            starting_checkpoint_id=snapshot.starting_checkpoint_id,
            tree_digest=snapshot.tree_digest,
            system_items=saved_systems(snapshot.system_releases_json),
            items=items,
            next_cursor=rows[limit - 1][0].entry_name if len(rows) > limit else None,
        )
