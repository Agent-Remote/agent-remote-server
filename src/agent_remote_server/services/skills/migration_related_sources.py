"""
使用确切历史目录成员与创建纪元验证迁移关联来源，不把内容相等当成身份授权。
"""

from dataclasses import dataclass

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import SkillCheckpoint, SkillDirectoryMember
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.finalization_checkpoints import valid_candidate_names
from agent_remote_server.services.skills.publication_context import (
    PublicationContext,
    subtree_entries,
)
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.migration_resolution import (
    MigrationResolutionInputs,
    migration_resolution_unit,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@dataclass
class MigrationRelatedSourceValidator:
    """
    调用方持有同一用户锁，本层只验证明确成员而不选择新的来源或版本。
    """

    queries: SkillStateQueryService
    repository: SkillPublicationRepository
    store: PrivateObjectStore

    async def validate(
        self,
        migration: SkillBranchPreparation,
        inputs: MigrationResolutionInputs,
        choices: list[SkillResolutionChoice],
        result: SkillTreeManifest,
    ) -> None:
        """
        实际修改及整体导入的关联身份都必须有历史证据，未改动成员不算发布写入。

        :param migration (SkillBranchPreparation): 已授权原始迁移
        :param inputs (MigrationResolutionInputs): 保存四侧和真实成员名称
        :param choices (list[SkillResolutionChoice]): 完整已验证非重叠选择
        :param result (SkillTreeManifest): 完整有效候选
        """
        runtime = self.queries.runtime
        candidate_names = await valid_candidate_names(self.store, migration.user_id, result)
        for name in candidate_names - set(inputs.names):
            if subtree_entries(inputs.directory, name) != subtree_entries(result, name):
                raise SkillContentError(
                    "STATE_SCOPE_MISMATCH", "migration cannot introduce an unrelated skill source"
                )
        saved_directory = await runtime.checkpoint(
            migration.user_id, migration.account_id, migration.directory_checkpoint_id
        )
        assert saved_directory is not None
        members = {member.entry_name: member for member in await runtime.members(saved_directory)}
        related = (set(migration_resolution_unit(inputs)) & set(members)) - {inputs.name}
        imported = related if any(choice.use == "incoming" for choice in choices) else set()
        changed = {
            name
            for name in members.keys() - {inputs.name}
            if subtree_entries(inputs.directory, name) != subtree_entries(result, name)
        }
        incoming = await self._incoming(migration, inputs) if imported else {}
        context = PublicationContext(
            self.queries.library, self.repository, runtime, self.queries.content
        )
        for name in sorted(changed | imported):
            member = members[name]
            current = await self._item(member)
            branch = await self.repository.branch(
                migration.user_id, migration.account_id, member.state_id
            )
            if current.state_epoch != branch.epoch:
                raise SkillContentError("STATE_EPOCH_CHANGED", "related source epoch changed")
            reason = await context.source_invalid(branch)
            if reason is not None:
                raise SkillContentError(
                    "SOURCE_CHANGED",
                    "related source is no longer active",
                    details={"reason": reason},
                )
            if name in imported:
                source = incoming.get(name)
                if source is None or source.state_id != member.state_id:
                    raise SkillContentError(
                        "SKILL_SOURCE_CONFLICT",
                        "incoming related source has another identity or revision",
                    )
                original = await self._item(source)
                if original.state_epoch != current.state_epoch:
                    raise SkillContentError(
                        "STATE_EPOCH_CHANGED", "incoming related source predates reset"
                    )
            if name in changed:
                if branch.head_checkpoint_id != current.id:
                    raise SkillContentError(
                        "STATE_PRECONDITION_CHANGED", "related branch head changed"
                    )
                if branch.expired or not current.retained:
                    raise SkillContentError("STATE_EXPIRED", "related branch is no longer retained")

    async def _item(self, member: SkillDirectoryMember) -> SkillCheckpoint:
        """
        读取精确历史成员而非当前 head，旧记录缺失纪元时拒绝猜测。

        :param member (SkillDirectoryMember): 已授权完整目录成员引用
        :return SkillCheckpoint: 保存原纪元的同源单项
        """
        item = await self.queries.runtime.checkpoint(
            member.user_id, member.account_id, member.checkpoint_id
        )
        if item is None or item.state_epoch is None:
            raise SkillContentError(
                "STATE_PROVENANCE_UNAVAILABLE", "related checkpoint epoch is unknown"
            )
        if item.state_id != member.state_id or item.subtree_prefix != member.entry_name:
            raise SkillContentError("STATE_SCOPE_MISMATCH", "related checkpoint identity differs")
        return item

    async def _incoming(
        self, migration: SkillBranchPreparation, inputs: MigrationResolutionInputs
    ) -> dict[str, SkillDirectoryMember]:
        """
        沿来源检查点的显式 backing 引用找成员，相同树的其他目录绝不可替代。

        :param migration (SkillBranchPreparation): 已授权保存迁移
        :param inputs (MigrationResolutionInputs): 原始四侧及名称
        :return dict[str, SkillDirectoryMember]: 来源真实完整目录成员
        """
        if migration.source_checkpoint_id is None:
            raise SkillContentError(
                "STATE_PROVENANCE_UNAVAILABLE", "incoming has no source checkpoint identity"
            )
        runtime = self.queries.runtime
        item = await runtime.checkpoint(
            migration.user_id, migration.account_id, migration.source_checkpoint_id
        )
        if item is None or item.state_epoch is None or item.backing_directory_id is None:
            raise SkillContentError(
                "STATE_PROVENANCE_UNAVAILABLE", "incoming backing provenance is unknown"
            )
        if (
            item.state_id != migration.source_state_id
            or item.subtree_prefix != inputs.name
            or item.content_digest != migration.incoming_digest
        ):
            raise SkillContentError(
                "STATE_SCOPE_MISMATCH", "incoming checkpoint differs from saved migration"
            )
        if item.state_epoch != migration.source_epoch:
            raise SkillContentError(
                "STATE_EPOCH_CHANGED", "incoming checkpoint predates source reset"
            )
        directory = await runtime.checkpoint(
            migration.user_id, migration.account_id, item.backing_directory_id
        )
        if (
            directory is None
            or directory.scope != "directory"
            or directory.content_digest != item.content_digest
        ):
            raise SkillContentError(
                "STATE_PROVENANCE_UNAVAILABLE", "incoming backing directory is unavailable"
            )
        return {member.entry_name: member for member in await runtime.members(directory)}
