"""
按最后成功来源检查点计算显式版本增量，不重复应用既有学习数据。
"""

from dataclasses import dataclass
from uuid import UUID

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.schemas.skill_migration import SkillMigrationPrecondition
from agent_remote_server.services.skills.branch_publication import BranchPublicationPlan
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.finalization_checkpoints import (
    validate_finalization_limits,
)
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.preparation_plan import replace_member, within_member
from agent_remote_server.skill_manager.directory_merge import (
    directory_merge_units,
    merge_directory_manifests,
)
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass
class IncrementalMigrationPlan:
    """
    三侧完整输入与可选完整发布，任何冲突都没有部分可用结果。
    """

    base: SkillTreeManifest
    current: SkillTreeManifest
    incoming: SkillTreeManifest
    directory: SkillTreeManifest
    publication: BranchPublicationPlan | None
    unchanged: bool
    conflicts: tuple[SkillMergeConflict, ...] = ()


@dataclass
class IncrementalMigrationPlanner:
    """
    预览和正式执行使用相同实际内容、依赖及总额度验证。
    """

    content: MigrationContent
    policy: SkillStoragePolicy

    async def prepare(
        self, user_id: UUID, state: SkillMigrationPrecondition
    ) -> IncrementalMigrationPlan:
        """
        上次成功来源作为基线，当前侧始终从目标分支自身读取。

        :param user_id (UUID): 内容所有者
        :param state (SkillMigrationPrecondition): 完整双方状态与上次成功基线
        :return IncrementalMigrationPlan: 完整计算结果或保留冲突
        """
        queries = self.content.queries
        if state.source.expired or state.target.expired:
            raise SkillContentError(
                "STATE_EXPIRED", "migration branch requires explicit reset or restore"
            )
        assert state.source.checkpoint_id is not None
        incoming = await self.content.tree(
            await queries.require(user_id, state.source.checkpoint_id)
        )
        if state.last_migrated_checkpoint_id is None:
            base, _ = await self.content.original(
                user_id, state.skill_id, state.source.revision_id, state.name
            )
        else:
            base = await self.content.tree(
                await queries.require(user_id, state.last_migrated_checkpoint_id)
            )
        if state.target.checkpoint_id is None:
            current, _ = await self.content.original(
                user_id, state.skill_id, state.target.revision_id, state.name
            )
        else:
            current = await self.content.tree(
                await queries.require(user_id, state.target.checkpoint_id)
            )
        directory = await queries.require(user_id, state.directory_checkpoint_id)
        directory_tree = await self.content.tree(directory)
        members = list(await queries.runtime.members(directory))
        unchanged = (
            state.last_migrated_checkpoint_id == state.source.checkpoint_id
            and state.target.checkpoint_id is not None
        )
        result = None
        conflicts: tuple[SkillMergeConflict, ...] = ()
        if unchanged:
            result = directory_tree
        else:
            names = {member.entry_name for member in members} | {state.name}
            units = directory_merge_units((base, current, incoming, directory_tree), names)
            linked = next((unit for unit in units if state.name in unit and len(unit) > 1), None)
            if linked is not None:
                conflicts = (
                    SkillMergeConflict(path=state.name, reason="source_conflict", unit=linked),
                )
            else:
                base, current, incoming = (
                    _member(tree, state.name) for tree in (base, current, incoming)
                )
                merged = merge_directory_manifests(base, current, incoming, {state.name})
                conflicts = merged.conflicts
                if merged.merged is not None:
                    try:
                        result = replace_member(directory_tree, merged.merged, state.name)
                    except ValueError:
                        conflicts = (
                            SkillMergeConflict(
                                path=state.name, reason="invalid_tree", unit=(state.name,)
                            ),
                        )
        for tree in (base, current, incoming):
            await self.content.verify(user_id, tree)
        if result is not None:
            validate_finalization_limits(
                result, {member.entry_name for member in members} | {state.name}, self.policy
            )
            await self.content.verify(user_id, result)
        await queries.content.validate_state_admissions(
            user_id, (base, current, incoming) + ((result,) if result else ())
        )
        publication = (
            BranchPublicationPlan(state.name, directory, state.directory_epoch, members, result)
            if result is not None
            else None
        )
        return IncrementalMigrationPlan(
            base, current, incoming, directory_tree, publication, unchanged, conflicts
        )


def _member(tree: SkillTreeManifest, name: str) -> SkillTreeManifest:
    """
    只有已证明没有外部关联时才裁定独立成员比较树。

    :param tree (SkillTreeManifest): 原始完整树
    :param name (str): 目标成员名
    :return SkillTreeManifest: 保留原始路径的完整独立树
    """
    return SkillTreeManifest(
        entries=tuple(entry for entry in tree.entries if within_member(entry.path, name))
    )
