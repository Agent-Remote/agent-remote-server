"""
按真实使用历史规划首次版本迁移，保留完整输入而不提前改动 head。
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from agent_remote_server.models.skill_state import (
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.repositories.skill_preparation import SkillPreparationRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.schemas.skill_state_commands import SkillCurrentStateView
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.finalization_checkpoints import (
    validate_finalization_limits,
)
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.directory_merge import (
    directory_merge_units,
    merge_directory_manifests,
)
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass
class BranchPreparationPlan:
    """
    完整计划中的引用都来自同一个已授权配置视图。
    """

    mode: Literal["initial", "forward", "older", "resume"]
    directory: SkillCheckpoint
    members: list[SkillDirectoryMember]
    source: AccountSkillState | None
    source_checkpoint: SkillCheckpoint | None
    base: SkillTreeManifest
    current: SkillTreeManifest
    incoming: SkillTreeManifest
    result: SkillTreeManifest | None
    conflicts: tuple[SkillMergeConflict, ...] = ()


@dataclass
class BranchPreparationPlanner:
    """
    纯预览和实际受理共用来源选择、内容校验及保守关联单元规则。
    """

    queries: SkillStateQueryService
    repository: SkillPreparationRepository
    content: MigrationContent
    policy: SkillStoragePolicy

    async def prepare(
        self, user_id: UUID, selection: SkillCurrentStateView
    ) -> BranchPreparationPlan:
        """
        优先复用目标 head，首次向前进入才从最后有效分支迁移。

        :param user_id (UUID): 已认证所有者
        :param selection (SkillCurrentStateView): 完整当前选择
        :return BranchPreparationPlan: 无数据库写入的完整计划
        """
        target = selection.precondition.targets[0]
        if target.origin != "user_library" or not target.rule.included:
            raise SkillContentError(
                "STATE_SCOPE_MISMATCH", "prepare requires an enabled library source"
            )
        if target.expired:
            raise SkillContentError("STATE_EXPIRED", "target requires explicit reset or restore")
        local = await self.queries.repository.local_source(
            user_id, selection.selector.account_id, target.name
        )
        if local is not None:
            raise SkillContentError(
                "SKILL_SOURCE_CONFLICT", "account-local source occupies this name"
            )
        head_id = selection.precondition.directory_head_id
        if selection.precondition.directory_mode != "managed_v1" or head_id is None:
            raise SkillContentError("STATE_NOT_MANAGED", "account takeover must complete first")
        directory = await self.queries.require(user_id, head_id)
        directory_tree = await self.content.tree(directory)
        members = list(await self.queries.runtime.members(directory))
        if target.head_checkpoint_id is not None:
            checkpoint = await self.queries.require(user_id, target.head_checkpoint_id)
            tree = await self.content.tree(checkpoint)
            await self.content.verify(user_id, tree)
            await self.queries.content.validate_state_admissions(user_id, (tree,))
            return BranchPreparationPlan(
                "resume", directory, members, None, None, tree, tree, tree, tree
            )
        source = await self.repository.effective(
            user_id, directory.account_id, target.skill_id, target.installation_epoch
        )
        current, target_number = await self.content.original(
            user_id, target.skill_id, target.revision_id, target.name
        )
        if source is None:
            if await self.repository.has_unrecorded_use(
                user_id, directory.account_id, target.skill_id, target.installation_epoch
            ):
                raise SkillContentError(
                    "STATE_HISTORY_UNAVAILABLE",
                    "previous snapshots exist without reliable effective-branch history",
                )
            plan = BranchPreparationPlan(
                "initial", directory, members, None, None, current, current, current, current
            )
        else:
            if source.expired or source.head_checkpoint_id is None:
                raise SkillContentError(
                    "STATE_EXPIRED", "last effective branch requires explicit recovery"
                )
            source_checkpoint = await self.queries.require(user_id, source.head_checkpoint_id)
            source_tree = await self.content.tree(source_checkpoint)
            assert source.base_revision_id is not None
            base, source_number = await self.content.original(
                user_id, target.skill_id, source.base_revision_id, target.name
            )
            if target_number < source_number:
                plan = BranchPreparationPlan(
                    "older",
                    directory,
                    members,
                    source,
                    source_checkpoint,
                    base,
                    current,
                    source_tree,
                    current,
                )
            else:
                plan = _forward(
                    directory,
                    members,
                    source,
                    source_checkpoint,
                    base,
                    current,
                    source_tree,
                    directory_tree,
                    target.name,
                )
        for tree in (plan.base, plan.current, plan.incoming):
            await self.content.verify(user_id, tree)
        if plan.result is not None:
            try:
                plan.result = replace_member(directory_tree, plan.result, target.name)
            except ValueError:
                plan.result = None
                plan.conflicts = (
                    SkillMergeConflict(
                        path=target.name, reason="invalid_tree", unit=(target.name,)
                    ),
                )
        if plan.result is not None:
            validate_finalization_limits(
                plan.result, {member.entry_name for member in members} | {target.name}, self.policy
            )
            await self.content.verify(user_id, plan.result)
        await self.queries.content.validate_state_admissions(
            user_id,
            (plan.base, plan.current, plan.incoming)
            + ((plan.result,) if plan.result is not None else ()),
        )
        return plan


def _forward(
    directory: SkillCheckpoint,
    members: list[SkillDirectoryMember],
    source: AccountSkillState,
    checkpoint: SkillCheckpoint,
    base: SkillTreeManifest,
    current: SkillTreeManifest,
    incoming: SkillTreeManifest,
    directory_tree: SkillTreeManifest,
    name: str,
) -> BranchPreparationPlan:
    """
    只自动迁移独立成员，任何外部链接关联都保留完整来源供后续解决。

    :param directory (SkillCheckpoint): 当前完整目录
    :param members (list[SkillDirectoryMember]): 当前稳定成员
    :param source (AccountSkillState): 真实上次使用分支
    :param checkpoint (SkillCheckpoint): 本次来源 head
    :param base (SkillTreeManifest): 旧原始包
    :param current (SkillTreeManifest): 新原始包
    :param incoming (SkillTreeManifest): 旧发布完整树
    :param directory_tree (SkillTreeManifest): 当前目录的实际依赖关系
    :param name (str): 唯一可变更成员
    :return BranchPreparationPlan: 完整自动结果或明确关联冲突
    """
    names = {member.entry_name for member in members} | {name}
    units = directory_merge_units((base, current, incoming, directory_tree), names)
    linked = next((unit for unit in units if name in unit and len(unit) > 1), None)
    if linked is not None:
        return BranchPreparationPlan(
            "forward",
            directory,
            members,
            source,
            checkpoint,
            base,
            current,
            incoming,
            None,
            (SkillMergeConflict(path=name, reason="source_conflict", unit=linked),),
        )
    isolated = SkillTreeManifest(
        entries=tuple(entry for entry in incoming.entries if within_member(entry.path, name))
    )
    merged = merge_directory_manifests(base, current, isolated, {name})
    return BranchPreparationPlan(
        "forward",
        directory,
        members,
        source,
        checkpoint,
        base,
        current,
        isolated,
        merged.merged,
        merged.conflicts,
    )


def within_member(path: str, name: str) -> bool:
    """
    按组件边界判断目标成员，避免同名前缀混入。

    :param path (str): 相对路径
    :param name (str): 目标顶层名称
    :return bool: 是否属于目标成员
    """
    return path == name or path.startswith(name + "/")


def replace_member(
    directory: SkillTreeManifest, incoming: SkillTreeManifest, name: str
) -> SkillTreeManifest:
    """
    仅交换声明成员，其他来源和辅助根始终来自当前目录。

    :param directory (SkillTreeManifest): 当前账户目录
    :param incoming (SkillTreeManifest): 待采用目标树
    :param name (str): 明确成员名称
    :return SkillTreeManifest: 重新验证依赖的完整目录
    """
    entries = [entry for entry in directory.entries if not within_member(entry.path, name)]
    entries.extend(entry for entry in incoming.entries if within_member(entry.path, name))
    return SkillTreeManifest(entries=tuple(sorted(entries, key=lambda entry: entry.path.encode())))
