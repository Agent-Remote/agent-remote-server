"""
只读选择任意明确来源与目标版本，并固定上次成功增量基线。
"""

from dataclasses import dataclass
from uuid import UUID

from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.repositories.skill_preparation import SkillPreparationRepository
from agent_remote_server.schemas.skill_migration import (
    SkillMigrationBranch,
    SkillMigrationPrecondition,
    SkillMigrationSelector,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.services.tool_registry import ToolRegistry


@dataclass
class MigrationSelection:
    """
    版本选择独立于有效规则，授权和来源身份仍使用统一查询路径。
    """

    queries: SkillStateQueryService
    repository: SkillPreparationRepository

    async def current(
        self, user_id: UUID, selector: SkillMigrationSelector
    ) -> SkillMigrationPrecondition:
        """
        同账户同当前安装纪元的两个不同版本才允许建立显式迁移关系。

        :param user_id (UUID): 已认证用户
        :param selector (SkillMigrationSelector): 明确双方版本
        :return SkillMigrationPrecondition: 完整可比较的当前状态
        """
        skill_id = await self.queries.scope(user_id, selector.account_id, "item", selector.skill)
        assert skill_id is not None
        item = await self.queries.library.installation(user_id, str(skill_id))
        if item is None:
            raise SkillContentError(
                "STATE_SCOPE_MISMATCH", "revision migration requires a library source"
            )
        if item.removed:
            raise SkillContentError("SKILL_REMOVED", "migration source is not installed")
        if (
            await self.queries.repository.local_source(user_id, selector.account_id, item.name)
            is not None
        ):
            raise SkillContentError(
                "SKILL_SOURCE_CONFLICT", "account-local source occupies this name"
            )
        account = await self.queries.library.account(user_id, selector.account_id)
        assert account is not None
        if account.tool_type not in ToolRegistry.supported_tool_types():
            raise SkillContentError("UNSUPPORTED_TOOL", "account tool adapter is not registered")
        directory = await self.queries.runtime.directory(user_id, account.id)
        if (
            directory is None
            or directory.mode != "managed_v1"
            or directory.head_checkpoint_id is None
        ):
            raise SkillContentError("STATE_NOT_MANAGED", "account takeover must complete first")
        source_revision = await self.queries.library.revision(
            user_id, item.id, selector.from_revision
        )
        target_revision = await self.queries.library.revision(
            user_id, item.id, selector.to_revision
        )
        if source_revision is None or target_revision is None:
            raise SkillContentError(
                "REVISION_NOT_FOUND", "migration revision not found in this source"
            )
        if source_revision.id == target_revision.id:
            raise SkillContentError("INVALID_REQUEST", "migration requires two different revisions")
        source = await self.queries.runtime.branch(
            user_id, account.id, item.id, item.epoch, source_revision.id
        )
        target = await self.queries.runtime.branch(
            user_id, account.id, item.id, item.epoch, target_revision.id
        )
        if source is None or source.head_checkpoint_id is None:
            raise SkillContentError(
                "STATE_NOT_INITIALIZED", "migration source has no published state"
            )
        last = await self.repository.latest_migration(source, target, directory.epoch)
        return SkillMigrationPrecondition(
            account_id=account.id,
            skill_id=item.id,
            name=item.name,
            installation_epoch=item.epoch,
            library_generation=await self.queries.library.generation(user_id),
            directory_epoch=directory.epoch,
            directory_checkpoint_id=directory.head_checkpoint_id,
            source=_branch(source_revision.id, source),
            target=_branch(target_revision.id, target),
            last_migration_id=last.id if last else None,
            last_migrated_checkpoint_id=last.source_checkpoint_id if last else None,
            last_sequence=last.migration_sequence or 0 if last else 0,
            source_has_unmigrated_checkpoint=last is None
            or last.source_checkpoint_id != source.head_checkpoint_id,
        )


def _branch(revision_id: UUID, branch: AccountSkillState | None) -> SkillMigrationBranch:
    """
    分支缺失仍保持空值，不能因查询而初始化或解除过期。

    :param revision_id (UUID): 已授权不可变版本
    :param branch (AccountSkillState | None): 当前分支或缺失
    :return SkillMigrationBranch: 精确可比较前置条件
    """
    return SkillMigrationBranch(
        revision_id=revision_id,
        state_id=branch.id if branch else None,
        state_epoch=branch.epoch if branch else None,
        checkpoint_id=branch.head_checkpoint_id if branch else None,
        expired=branch.expired if branch else False,
    )
