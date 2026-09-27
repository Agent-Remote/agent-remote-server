"""
以原始迁移输入和最新同纪元目标重新比较，不加载旧选择或实时来源 head。
"""

from dataclasses import dataclass

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.services.skills.branch_publication import BranchPublicationPlan
from agent_remote_server.services.skills.finalization_checkpoints import (
    validate_finalization_limits,
)
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.migration_plan import IncrementalMigrationPlan
from agent_remote_server.services.skills.preparation_plan import replace_member, within_member
from agent_remote_server.skill_manager.directory_merge import (
    directory_merge_units,
    merge_directory_manifests,
)
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass
class MigrationRecomputePlanner:
    """
    只重建比较，不保存检查点、成功序号或选择授权。
    """

    content: MigrationContent
    policy: SkillStoragePolicy

    async def prepare(
        self, row: SkillBranchPreparation, target: AccountSkillState, name: str
    ) -> IncrementalMigrationPlan:
        """
        初次和旧版准备仅组合目标侧，向前与增量迁移重新做保守三方合并。

        :param row (SkillBranchPreparation): 已核对身份及全部纪元的原尝试
        :param target (AccountSkillState): 最新同纪元目标分支
        :param name (str): 原始稳定名称
        :return IncrementalMigrationPlan: 完整新比较和可选原子发布计划
        """
        queries = self.content.queries
        base = await queries.content.read_tree(row.user_id, "state", row.base_digest)
        current = (
            await self.content.tree(await queries.require(row.user_id, target.head_checkpoint_id))
            if target.head_checkpoint_id
            else await queries.content.read_tree(row.user_id, "state", row.current_digest)
        )
        incoming = (
            await self.content.tree(await queries.require(row.user_id, row.source_checkpoint_id))
            if row.source_checkpoint_id
            else await queries.content.read_tree(row.user_id, "state", row.incoming_digest)
        )
        directory_state = await queries.runtime.directory(row.user_id, row.account_id)
        assert directory_state is not None and directory_state.head_checkpoint_id is not None
        directory = await queries.require(row.user_id, directory_state.head_checkpoint_id)
        directory_tree = await self.content.tree(directory)
        members = list(await queries.runtime.members(directory))
        names = {member.entry_name for member in members} | {name}
        units = directory_merge_units((base, current, incoming, directory_tree), names)
        unit = next((unit for unit in units if name in unit), (name,))
        result = None
        conflicts: tuple[SkillMergeConflict, ...] = ()
        if row.mode in {"initial", "older"}:
            selected = current
        elif len(unit) > 1:
            selected = None
            conflicts = (SkillMergeConflict(path=name, reason="source_conflict", unit=unit),)
        else:
            merged = merge_directory_manifests(
                _member(base, name), _member(current, name), _member(incoming, name), {name}
            )
            selected, conflicts = merged.merged, merged.conflicts
        if selected is not None:
            try:
                result = replace_member(directory_tree, selected, name)
            except ValueError:
                conflicts = (SkillMergeConflict(path=name, reason="invalid_tree", unit=unit),)
        for tree in (base, current, incoming, directory_tree):
            await self.content.verify(row.user_id, tree)
        if result is not None:
            validate_finalization_limits(result, names, self.policy)
            await self.content.verify(row.user_id, result)
        await queries.content.validate_state_admissions(
            row.user_id, (base, current, incoming) + ((result,) if result else ())
        )
        publication = (
            BranchPublicationPlan(name, directory, row.directory_epoch, members, result)
            if result is not None
            else None
        )
        return IncrementalMigrationPlan(
            base, current, incoming, directory_tree, publication, False, conflicts
        )


def _member(tree: SkillTreeManifest, name: str) -> SkillTreeManifest:
    """
    确认单项没有外部关联后才提取比较内容，保存输入仍保持完整。

    :param tree (SkillTreeManifest): 完整来源树
    :param name (str): 明确单项名称
    :return SkillTreeManifest: 保留真实路径的独立子树
    """
    return SkillTreeManifest(
        entries=tuple(entry for entry in tree.entries if within_member(entry.path, name))
    )
