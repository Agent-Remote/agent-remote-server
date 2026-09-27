"""
只读规划迁移候选并核对关联来源，实际发布仍须在写锁内重查全部事务条件。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.repositories.skill_migration_resolution import (
    SkillMigrationResolutionRepository,
)
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_merge import SkillMergeResult
from agent_remote_server.schemas.skill_migration_conflicts import SkillMigrationConflictView
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.schemas.skill_state_queries import SkillStatePathDiff
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.finalization_checkpoints import (
    validate_finalization_limits,
)
from agent_remote_server.services.skills.migration_conflicts import SkillMigrationConflictService
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.migration_related_sources import (
    MigrationRelatedSourceValidator,
)
from agent_remote_server.services.skills.migration_resolution_choices import (
    choice_spec,
    load_choices,
)
from agent_remote_server.services.skills.publication_context import subtree_entries
from agent_remote_server.skill_manager.migration_resolution import (
    MigrationResolutionInputs,
    migration_resolution_unit,
    resolve_migration_conflicts,
)
from agent_remote_server.skill_manager.resolution import choices_overlap
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy
from agent_remote_server.skill_manager.tree_diff import manifest_difference


@dataclass(frozen=True)
class MigrationResolutionCalculation:
    """
    候选完整性和原始包修改分别说明，不声称其他关联来源已获发布授权。
    """

    info: SkillMigrationConflictView
    inputs: MigrationResolutionInputs
    unit: tuple[str, ...]
    result: SkillMergeResult
    target_revision_id: UUID
    target_modified: bool | None
    target_changes: list[SkillStatePathDiff] | None
    original_changes: list[SkillStatePathDiff] | None
    directory_changes: list[SkillStatePathDiff] | None
    other_changed_roots: tuple[str, ...]
    choices: tuple[SkillResolutionChoice, ...]


class MigrationResolutionPlanner:
    """
    在同一用户读锁下固定候选来源与完整覆盖，计算不会保存任何新对象。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        共享调用方事务、私有内容及迁移授权。

        :param session (AsyncSession): 外层事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 存储和目录限制
        """
        self.conflicts = SkillMigrationConflictService(session, store, policy)
        self.repository = SkillMigrationResolutionRepository(session)
        self._store = store
        self._policy = policy
        self._content = MigrationContent(self.conflicts.queries, store)
        self._related = MigrationRelatedSourceValidator(
            self.conflicts.queries, SkillPublicationRepository(session), store
        )

    async def calculate(
        self,
        user_id: UUID,
        migration_id: UUID,
        choices: list[SkillResolutionChoice],
        *,
        replacement: SkillResolutionChoice | None = None,
    ) -> MigrationResolutionCalculation:
        """
        对完整已授权选择计算候选，过期比较不能冒充今天可提交的预览。

        :param user_id (UUID): 已认证用户
        :param migration_id (UUID): 原始独立迁移身份
        :param choices (list[SkillResolutionChoice]): 调用方组合后的完整非重叠计划
        :param replacement (SkillResolutionChoice | None): 可选新选择，先替换重叠范围再验证
        :return MigrationResolutionCalculation: 完整候选及明确覆盖说明
        """
        row = await self.conflicts.require(user_id, migration_id)
        info = await self.conflicts.info(user_id, migration_id)
        if row.status != "conflicted":
            raise SkillContentError("CONFLICT_NOT_ACTIVE", "migration is no longer active")
        if info.live.recomputation_reasons:
            raise SkillContentError(
                "STATE_PRECONDITION_CHANGED",
                "migration comparison requires recomputation",
                details={"reasons": list(info.live.recomputation_reasons)},
            )
        inputs = await self.inputs(info, user_id)
        if replacement is not None:
            choices = [
                choice
                for choice in choices
                if not choices_overlap(choice, replacement, set(inputs.names))
            ] + [replacement]
        loaded = await load_choices(
            self.conflicts.queries.content, self._store, row, self.repository, choices
        )
        try:
            result = resolve_migration_conflicts(inputs, loaded)
        except ValueError as error:
            if isinstance(error, SkillContentError):
                raise
            raise SkillContentError("INVALID_RESOLUTION", str(error)) from error
        target_revision = info.live.target.revision_id
        original, _ = await self._content.original(
            user_id, row.installation_id, target_revision, info.name
        )
        await self._content.verify(user_id, original)
        target_changes = original_changes = directory_changes = None
        changed_roots: tuple[str, ...] = ()
        if result.merged is not None:
            await self._related.validate(row, inputs, choices, result.merged)
            validate_finalization_limits(result.merged, set(inputs.names), self._policy)
            await self._content.verify(user_id, result.merged)
            await self.conflicts.queries.content.validate_state_admissions(
                user_id, (result.merged,)
            )
            target_changes = manifest_difference(inputs.current, result.merged, info.name)
            original_changes = manifest_difference(original, result.merged, info.name)
            directory_changes = manifest_difference(inputs.directory, result.merged, None)
            changed_roots = tuple(
                sorted(
                    {change.path.split("/", 1)[0] for change in directory_changes} - {info.name},
                    key=str.encode,
                )
            )
        return MigrationResolutionCalculation(
            info,
            inputs,
            migration_resolution_unit(inputs),
            result,
            target_revision,
            bool(original_changes) if original_changes is not None else None,
            target_changes,
            original_changes,
            directory_changes,
            changed_roots,
            tuple(choices),
        )

    async def inputs(
        self, info: SkillMigrationConflictView, user_id: UUID
    ) -> MigrationResolutionInputs:
        """
        使用受理时真实目录成员分组，额外入口不能仅凭路径取得独立身份或额度。

        :param info (SkillMigrationConflictView): 原迁移及实时诊断
        :param user_id (UUID): 已认证用户
        :return MigrationResolutionInputs: 完整保存输入与原目录真实成员范围
        """
        queries = self.conflicts.queries
        trees: list[SkillTreeManifest] = []
        for side in (info.base, info.current, info.incoming, info.directory):
            if side.tree_digest is None:
                raise SkillContentError("STATE_EXPIRED", "saved migration input is not retained")
            tree = await queries.content.read_tree(user_id, "state", side.tree_digest)
            await self._content.verify(user_id, tree)
            trees.append(tree)
        assert info.directory.checkpoint_id is not None
        directory = await queries.require(user_id, info.directory.checkpoint_id)
        names = {member.entry_name for member in await queries.runtime.members(directory)} | {
            info.name
        }
        return MigrationResolutionInputs(
            info.name,
            frozenset(names),
            trees[0],
            trees[1],
            trees[2],
            trees[3],
            info.original.conflicts,
        )

    async def related_writes(
        self, row: SkillBranchPreparation, inputs: MigrationResolutionInputs
    ) -> set[str]:
        """
        只读取旧计划确定写入边界，不把旧选择或人工授权转移到新比较。

        :param row (SkillBranchPreparation): 已授权原始尝试
        :param inputs (MigrationResolutionInputs): 原始保存四侧
        :return set[str]: 实际候选改动成员，未形成候选时保守采用输入差异
        """
        choices = [choice_spec(choice) for choice in await self.repository.choices(row)]
        loaded = await load_choices(
            self.conflicts.queries.content, self._store, row, self.repository, choices
        )
        result = resolve_migration_conflicts(inputs, loaded)
        candidate = result.merged if result.merged is not None else inputs.incoming
        return {
            name
            for name in set(migration_resolution_unit(inputs)) - {inputs.name, "."}
            if subtree_entries(inputs.directory, name) != subtree_entries(candidate, name)
        }
