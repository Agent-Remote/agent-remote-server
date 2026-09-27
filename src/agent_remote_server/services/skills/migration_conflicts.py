"""
读取迁移专用冲突和原始侧身份，实时诊断不替换保存内容。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.repositories.skill_migration_conflicts import (
    SkillMigrationConflictRepository,
)
from agent_remote_server.repositories.skill_preparation import SkillPreparationRepository
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_migration_conflicts import (
    MigrationInputSource,
    SkillMigrationConflictPage,
    SkillMigrationConflictSummary,
    SkillMigrationConflictView,
    SkillMigrationDrift,
    SkillMigrationInput,
    SkillMigrationLiveBranch,
)
from agent_remote_server.schemas.skill_preparation import SkillPreparationView
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillMigrationConflictService:
    """
    查询仅取得既有用户读锁，不创建库、分支、上传或解决计划。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        共享外层事务的授权与一致内容视图。

        :param session (AsyncSession): 请求事务
        :param store (PrivateObjectStore): 私有对象卷
        :param policy (SkillStoragePolicy): 存储策略
        """
        self.queries = SkillStateQueryService(session, store, policy)
        self.repository = SkillMigrationConflictRepository(session)
        self.preparations = SkillPreparationRepository(session)

    async def require(self, user_id: UUID, migration_id: UUID) -> SkillBranchPreparation:
        """
        仅原始受理为冲突的迁移才可从此域读取，成功准备不冒充冲突。

        :param user_id (UUID): 已认证用户
        :param migration_id (UUID): 原始受理身份
        :return SkillBranchPreparation: 已授权且真实发生过冲突的记录
        """
        await self.queries.library.read_library(user_id)
        row = await self.repository.record(user_id, migration_id)
        if row is None or (original(row).status != "conflicted" and row.recomputed_from_id is None):
            raise SkillContentError("CONFLICT_NOT_FOUND", "migration conflict not found")
        return row

    async def list(
        self,
        user_id: UUID,
        account_id: UUID,
        skill: str | None = None,
        limit: int = 100,
        cursor: UUID | None = None,
    ) -> SkillMigrationConflictPage:
        """
        可按归档稳定身份查询，跨账户或来源的游标不能替用。

        :param user_id (UUID): 已认证用户
        :param account_id (UUID): 指定账户
        :param skill (str | None): 可选稳定来源或当前名称
        :param limit (int): 页大小
        :param cursor (UUID | None): 上一页末尾身份
        :return SkillMigrationConflictPage: 稳定有界冲突页
        """
        if not 1 <= limit <= 200:
            raise SkillContentError("INVALID_REQUEST", "page size is out of range")
        skill_id = await self.queries.scope(
            user_id, account_id, "item" if skill is not None else "account-directory", skill
        )
        before = await self.require(user_id, cursor) if cursor else None
        if before is not None and (
            before.account_id != account_id
            or (skill_id is not None and before.installation_id != skill_id)
        ):
            raise SkillContentError("CONFLICT_NOT_FOUND", "cursor not found in this scope")
        rows = await self.repository.conflicts(user_id, account_id, skill_id, limit + 1, before)
        return SkillMigrationConflictPage(
            items=[summary(row) for row in rows[:limit]],
            next_cursor=rows[limit - 1].id if len(rows) > limit else None,
        )

    async def info(self, user_id: UUID, migration_id: UUID) -> SkillMigrationConflictView:
        """
        原始响应及真实四侧与当前漂移分别返回，不执行迁移或重算。

        :param user_id (UUID): 当前用户
        :param migration_id (UUID): 原始迁移身份
        :return SkillMigrationConflictView: 完整保存输入及实时诊断
        """
        row = await self.require(user_id, migration_id)
        saved = original(row)
        target = await self.repository.branch(row, row.target_state_id)
        source = (
            await self.repository.branch(row, row.source_state_id) if row.source_state_id else None
        )
        directory = await self.queries.require(user_id, row.directory_checkpoint_id)
        return SkillMigrationConflictView(
            **summary(row).model_dump(),
            original=saved,
            base=side_input(row, saved.base_source, row.base_digest, source, target),
            current=side_input(row, saved.current_source, row.current_digest, source, target),
            incoming=side_input(row, saved.incoming_source, row.incoming_digest, source, target),
            directory=SkillMigrationInput(
                source="account_directory",
                revision_id=None,
                checkpoint_id=directory.id,
                tree_digest=directory.tree_digest
                if directory.retained and row.content_retired_at is None
                else None,
            ),
            live=await self._drift(row, saved, source, target),
        )

    async def _drift(
        self,
        row: SkillBranchPreparation,
        saved: SkillMigrationView | SkillPreparationView,
        source: AccountSkillState | None,
        target: AccountSkillState,
    ) -> SkillMigrationDrift:
        """
        来源同纪元的新 head 单独提示，重置和目标变化才使旧比较失效。

        :param row (SkillBranchPreparation): 已授权原始记录
        :param saved (SkillMigrationView | SkillPreparationView): 原始不可变响应
        :param source (AccountSkillState | None): 原来源当前分支
        :param target (AccountSkillState): 原目标当前分支
        :return SkillMigrationDrift: 不含内容的当前条件与重算原因
        """
        directory = await self.queries.runtime.directory(row.user_id, row.account_id)
        item = await self.queries.library.installation(row.user_id, str(row.installation_id))
        assert item is not None
        generation = await self.queries.library.generation(row.user_id)
        last = (
            await self.preparations.latest_migration(source, target, directory.epoch)
            if source and directory
            else None
        )
        expected_last = (
            saved.before.last_migration_id if isinstance(saved, SkillMigrationView) else None
        )
        expected_head = (
            saved.before.target.checkpoint_id
            if isinstance(saved, SkillMigrationView)
            else saved.before.targets[0].head_checkpoint_id
        )
        expected_directory = row.directory_checkpoint_id
        if row.status == "ready":
            assert row.result_directory_id is not None
            expected_head = row.result_checkpoint_id
            expected_directory = row.result_directory_id
            if row.migration_sequence is not None:
                expected_last = row.id
        conditions = {
            "superseded": row.status == "superseded",
            "installation_removed": item.removed,
            "installation_epoch_changed": item.epoch != row.installation_epoch,
            "library_generation_changed": generation != row.library_generation,
            "directory_mode_changed": directory is None or directory.mode != "managed_v1",
            "directory_epoch_changed": directory is None or directory.epoch != row.directory_epoch,
            "directory_head_changed": directory is None
            or directory.head_checkpoint_id != expected_directory,
            "source_epoch_changed": source is not None and source.epoch != row.source_epoch,
            "source_expired": source is not None and source.expired,
            "target_epoch_changed": target.epoch != row.target_epoch,
            "target_head_changed": target.head_checkpoint_id != expected_head,
            "target_expired": target.expired,
            "migration_baseline_changed": (last.id if last else None) != expected_last,
        }
        return SkillMigrationDrift(
            source=live_branch(source) if source else None,
            target=live_branch(target),
            directory_mode=directory.mode if directory else None,
            directory_epoch=directory.epoch if directory else None,
            directory_checkpoint_id=directory.head_checkpoint_id if directory else None,
            library_generation=generation,
            installation_epoch=item.epoch,
            installation_removed=item.removed,
            last_migration_id=last.id if last else None,
            source_head_advanced=bool(
                source
                and source.epoch == row.source_epoch
                and source.head_checkpoint_id != row.source_checkpoint_id
            ),
            recomputation_reasons=tuple(
                reason for reason, changed in conditions.items() if changed
            ),
        )


def original(row: SkillBranchPreparation) -> SkillMigrationView | SkillPreparationView:
    """
    按原模式解释不可变响应，不将增量基线误标为会话快照。

    :param row (SkillBranchPreparation): 已授权保存记录
    :return SkillMigrationView | SkillPreparationView: 对应模式的原始响应
    """
    if row.mode == "incremental":
        return SkillMigrationView.model_validate(row.response_json)
    return SkillPreparationView.model_validate(row.response_json)


def summary(row: SkillBranchPreparation) -> SkillMigrationConflictSummary:
    """
    摘要使用受理时名称，不随今天的库配置重解释。

    :param row (SkillBranchPreparation): 已授权保存记录
    :return SkillMigrationConflictSummary: 不包含正文的稳定身份摘要
    """
    saved = original(row)
    name = (
        saved.before.name if isinstance(saved, SkillMigrationView) else saved.before.targets[0].name
    )
    return SkillMigrationConflictSummary(
        id=row.id,
        account_id=row.account_id,
        skill_id=row.installation_id,
        installation_epoch=row.installation_epoch,
        name=name,
        mode=row.mode,
        status=row.status,
        source_state_id=row.source_state_id,
        source_epoch=row.source_epoch,
        target_state_id=row.target_state_id,
        target_epoch=row.target_epoch,
        directory_epoch=row.directory_epoch,
        created_at=row.created_at,
        recomputed_from_id=row.recomputed_from_id,
        replacement_id=row.replacement_id,
        superseded_reason=row.superseded_reason,
    )


def live_branch(branch: AccountSkillState) -> SkillMigrationLiveBranch:
    """
    仅库来源参与跨版本迁移，返回精确当前 head 和纪元。

    :param branch (AccountSkillState): 已授权库来源分支
    :return SkillMigrationLiveBranch: 实时元数据
    """
    assert branch.base_revision_id is not None
    return SkillMigrationLiveBranch(
        state_id=branch.id,
        revision_id=branch.base_revision_id,
        epoch=branch.epoch,
        checkpoint_id=branch.head_checkpoint_id,
        expired=branch.expired,
    )


def side_input(
    row: SkillBranchPreparation,
    label: MigrationInputSource,
    digest: str,
    source: AccountSkillState | None,
    target: AccountSkillState,
) -> SkillMigrationInput:
    """
    根据保存标签选择原分支身份，不能把同摘要当成来源等价。

    :param row (SkillBranchPreparation): 已授权受理记录
    :param label (MigrationInputSource): 保存侧的真实含义
    :param digest (str): 保存的完整树摘要
    :param source (AccountSkillState | None): 原来源分支
    :param target (AccountSkillState): 原目标分支
    :return SkillMigrationInput: 不可变来源引用
    """
    branch = (
        source
        if label in {"old_original", "last_migrated", "old_published", "source_published"}
        else target
    )
    checkpoints = {
        "last_migrated": row.base_checkpoint_id,
        "old_published": row.source_checkpoint_id,
        "source_published": row.source_checkpoint_id,
        "target_published": row.current_checkpoint_id,
        "existing_target": row.result_checkpoint_id,
    }
    return SkillMigrationInput(
        source=label,
        revision_id=branch.base_revision_id if branch else None,
        checkpoint_id=checkpoints.get(label),
        tree_digest=digest if row.content_retired_at is None else None,
    )
