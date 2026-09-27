"""
把完整迁移候选转换成精确多分支发布计划和逐分支覆盖预览。
"""

from dataclasses import dataclass
from uuid import UUID

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_migration_resolution import SkillMigrationResolutionBranch
from agent_remote_server.schemas.skill_state_queries import SkillStatePathDiff
from agent_remote_server.services.skills.branch_publication import (
    BranchPublicationPlan,
    SkillBranchPublisher,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.migration_resolution_plan import (
    MigrationResolutionCalculation,
)
from agent_remote_server.services.skills.publication_context import subtree_entries
from agent_remote_server.services.skills.state_diff import StateDiffService
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.tree_diff import manifest_difference


@dataclass(frozen=True)
class MigrationResolutionPublication:
    """
    当前写锁内确定的精确目标、关联写入集合及完整内容，不能跨事务当作授权重用。
    """

    target: AccountSkillState
    related: dict[str, AccountSkillState]
    plan: BranchPublicationPlan
    affected: list[SkillMigrationResolutionBranch]


@dataclass
class MigrationResolutionApply:
    """
    预览不写引用，实际提交由外层用户锁和保存点保护。
    """

    queries: SkillStateQueryService
    repository: SkillPublicationRepository
    local: SkillLocalRepository
    store: PrivateObjectStore

    async def prepare(
        self, migration: SkillBranchPreparation, calculation: MigrationResolutionCalculation
    ) -> MigrationResolutionPublication:
        """
        只选择明确目标及确实修改的稳定成员，分别列出自身 head 和原始版本差异。

        :param migration (SkillBranchPreparation): 同事务授权的迁移
        :param calculation (MigrationResolutionCalculation): 完整已验证候选
        :return MigrationResolutionPublication: 精确多分支计划及覆盖说明
        """
        result = calculation.result.merged
        assert result is not None
        target = await self.repository.branch(
            migration.user_id, migration.account_id, migration.target_state_id
        )
        expected = calculation.info.live.target
        if (target.epoch, target.head_checkpoint_id, target.base_revision_id, target.expired) != (
            expected.epoch,
            expected.checkpoint_id,
            calculation.target_revision_id,
            False,
        ):
            raise SkillContentError("HEAD_CHANGED", "target changed after resolution calculation")
        directory = await self.queries.require(migration.user_id, migration.directory_checkpoint_id)
        members = list(await self.queries.runtime.members(directory))
        assert calculation.target_changes is not None and calculation.original_changes is not None
        affected = [
            _view(
                target,
                calculation.inputs.name,
                calculation.target_changes,
                calculation.original_changes,
            )
        ]
        related = {}
        content = MigrationContent(self.queries, self.store)
        difference = StateDiffService(self.queries, self.local)
        for member in members:
            name = member.entry_name
            if name == calculation.inputs.name or name not in calculation.other_changed_roots:
                continue
            branch = await self.repository.branch(
                migration.user_id, migration.account_id, member.state_id
            )
            checkpoint = await self.queries.require(migration.user_id, member.checkpoint_id)
            if (
                branch.head_checkpoint_id != checkpoint.id
                or branch.epoch != checkpoint.state_epoch
                or branch.expired
            ):
                raise SkillContentError("HEAD_CHANGED", "related target changed after calculation")
            tree = await content.tree(checkpoint)
            if subtree_entries(tree, name) != subtree_entries(calculation.inputs.directory, name):
                raise SkillContentError(
                    "STATE_SCOPE_MISMATCH", "directory member differs from its checkpoint"
                )
            _, _, _, original = await difference.baseline(checkpoint)
            await content.verify(migration.user_id, original)
            related[name] = branch
            affected.append(
                _view(
                    branch,
                    name,
                    manifest_difference(tree, result, name),
                    manifest_difference(original, result, name),
                )
            )
        plan = BranchPublicationPlan(
            calculation.inputs.name, directory, migration.directory_epoch, members, result
        )
        return MigrationResolutionPublication(target, related, plan, affected)

    async def publish(
        self, publication: MigrationResolutionPublication, operation_id: UUID
    ) -> tuple[dict[str, UUID], UUID]:
        """
        完整关联单元和其他独立上下文一起发布，原始纪元不改变。

        :param publication (MigrationResolutionPublication): 本事务刚验证的精确计划
        :param operation_id (UUID): 本次解决操作身份
        :return tuple[dict[str, UUID], UUID]: 新单项视图及完整目录身份
        """
        return await SkillBranchPublisher(self.queries, self.repository, self.store).publish_many(
            publication.target,
            publication.plan,
            f"migration-resolution:{operation_id}",
            publication.related,
        )


def _view(
    branch: AccountSkillState,
    name: str,
    changes: list[SkillStatePathDiff],
    original: list[SkillStatePathDiff],
) -> SkillMigrationResolutionBranch:
    """
    固定分支身份不因导入旧内容改变，modified 单独说明相对其原始版本的修改。

    :param branch (AccountSkillState): 确切将写入分支
    :param name (str): 稳定名称
    :param changes (list[SkillStatePathDiff]): 相对自身当前侧的全部变化
    :param original (list[SkillStatePathDiff]): 相对自身原始版本的全部修改
    :return SkillMigrationResolutionBranch: 可确认的精确覆盖说明
    """
    skill_id = branch.installation_id or branch.local_skill_id
    revision_id = branch.base_revision_id or branch.local_revision_id
    assert skill_id is not None and revision_id is not None
    return SkillMigrationResolutionBranch(
        name=name,
        state_id=branch.id,
        skill_id=skill_id,
        origin="user_library" if branch.installation_id else "account_local",
        revision_id=revision_id,
        installation_epoch=branch.installation_epoch,
        state_epoch=branch.epoch,
        checkpoint_id=branch.head_checkpoint_id,
        result_checkpoint_id=None,
        changes=changes,
        original_changes=original,
        modified=bool(original),
    )
