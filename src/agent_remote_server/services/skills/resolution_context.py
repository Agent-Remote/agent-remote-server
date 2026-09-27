"""
在同一用户锁内固定解决输入、候选身份与发布前置条件。
"""

from dataclasses import dataclass
from uuid import UUID

from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_resolution import SkillResolutionRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.publication_context import (
    PublicationBranch,
    PublicationContext,
    directory_members,
)
from agent_remote_server.skill_manager.manifest import manifest_digest


@dataclass
class ResolutionInputs:
    """
    只在同一已锁定事务中使用，不能跨请求缓存授权或目标状态。
    """

    publication: SkillPublication
    receipt: SkillFinalization
    snapshot: SessionSkillSnapshot
    base: SkillTreeManifest
    current: SkillTreeManifest
    incoming: SkillTreeManifest
    conflicts: tuple[SkillMergeConflict, ...]
    branches: list[PublicationBranch]
    candidates: list[AccountLocalSkill]
    names: set[str]
    members: dict[str, tuple[UUID, UUID]]
    directory: AccountSkillDirectoryState | None
    stale_reason: str | None


@dataclass
class ResolutionContext:
    """
    用户计划访问与已有发布依赖共享事务。
    """

    publication: PublicationContext
    repository: SkillResolutionRepository
    local: SkillLocalRepository

    async def require(self, user_id: UUID, publication_id: UUID) -> SkillPublication:
        """
        猜测发布身份不能扩展用户或账户权限。

        :param user_id (UUID): 当前用户
        :param publication_id (UUID): 发布尝试身份
        :return SkillPublication: 当前用户有权检查的尝试
        """
        result = await self.repository.publication(user_id, publication_id)
        if result is None:
            raise SkillContentError("CONFLICT_NOT_FOUND", "conflict not found")
        return result

    async def load(self, publication: SkillPublication) -> ResolutionInputs:
        """
        加载固定三侧并比较所有保存的 head，不自动改变任何计划。

        :param publication (SkillPublication): 已授权的未解决尝试
        :return ResolutionInputs: 原始内容与是否需要重算的说明
        """
        context = self.publication
        user_id, account_id = publication.user_id, publication.account_id
        receipt = await context.repository.receipt(user_id, publication.finalization_id)
        assert receipt is not None and receipt.tree_digest is not None
        snapshot = await context.repository.snapshot(receipt)
        if publication.current_tree_digest is None or publication.expected_directory_id is None:
            raise SkillContentError("CONFLICT_NOT_ACTIVE", "attempt has no resolvable comparison")
        base = await context.content.read_tree(user_id, "state", snapshot.tree_digest)
        current = await context.content.read_tree(user_id, "state", publication.current_tree_digest)
        incoming = await context.content.read_tree(user_id, "state", receipt.tree_digest)
        expected = await context.runtime.checkpoint(
            user_id, account_id, publication.expected_directory_id
        )
        if expected is None:
            raise SkillContentError("STATE_EXPIRED", "comparison directory is not retained")
        branches, reason = await context.branches(snapshot, base, incoming)
        members = directory_members(list(await context.runtime.members(expected)), branches)
        assert receipt.checkpoint_id is not None
        candidates = list(await self.local.candidates(user_id, account_id, receipt.checkpoint_id))
        names = (
            set(members)
            | {branch.item.entry_name for branch in branches}
            | {item.name for item in candidates}
        )
        conflicts = tuple(
            SkillMergeConflict.model_validate(item) for item in publication.conflicts_json
        )
        directory = await context.runtime.directory(user_id, account_id)
        if (
            directory is None
            or directory.mode != "managed_v1"
            or directory.epoch != publication.directory_epoch
        ):
            reason = reason or "directory_epoch_changed"
        elif directory.head_checkpoint_id != publication.expected_directory_id:
            reason = reason or "directory_head_changed"
        saved = {item.state_id: item for item in await self.repository.branches(publication)}
        for branch in branches:
            previous = saved.get(branch.state.id)
            if previous is None or previous.state_epoch != branch.state.epoch:
                reason = reason or "state_epoch_changed"
            elif previous.expected_checkpoint_id != branch.state.head_checkpoint_id:
                reason = reason or "branch_head_changed"
        occupied = {item.name for item in await context.library.list_installations(user_id)}
        occupied |= await context.repository.active_local_names(user_id, account_id)
        previous_collisions = {item.path for item in conflicts if item.reason == "source_conflict"}
        if previous_collisions != {item.name for item in candidates if item.name in occupied}:
            reason = reason or "source_changed"
        if reason is None and expected.tree_digest is not None:
            actual = await context.content.read_tree(user_id, "state", expected.tree_digest)
            projection, _ = await context.current_tree(user_id, actual, branches)
            if manifest_digest(projection) != publication.current_tree_digest:
                reason = "comparison_changed"
        return ResolutionInputs(
            publication,
            receipt,
            snapshot,
            base,
            current,
            incoming,
            conflicts,
            branches,
            candidates,
            names,
            members,
            directory,
            reason,
        )
