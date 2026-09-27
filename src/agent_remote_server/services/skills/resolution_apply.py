"""
校验人工完整结果的来源边界，并复用目录原子发布。
"""

from dataclasses import dataclass, replace
from uuid import uuid4

from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.finalization_checkpoints import (
    valid_candidate_names,
    validate_finalization_limits,
)
from agent_remote_server.services.skills.local_candidates import LocalSkillCandidateService
from agent_remote_server.services.skills.publication_apply import PublicationApply
from agent_remote_server.services.skills.publication_context import (
    PublicationBranch,
    subtree_entries,
)
from agent_remote_server.services.skills.resolution_context import (
    ResolutionContext,
    ResolutionInputs,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass
class ResolvedTargets:
    """
    预览阶段只确定写入目标，不创建候选或内容引用。
    """

    branches: list[PublicationBranch]
    candidates: list[AccountLocalSkill]
    new_names: set[str]


@dataclass
class ResolutionApply:
    """
    最终结果与计划共用用户锁，任何失败由外层回滚整个命令。
    """

    context: ResolutionContext
    store: PrivateObjectStore
    policy: SkillStoragePolicy
    candidates: LocalSkillCandidateService

    async def validate(
        self,
        inputs: ResolutionInputs,
        result: SkillTreeManifest,
        choices: list[SkillResolutionChoice],
    ) -> ResolvedTargets:
        """
        按实际将写入的内容重查来源、原始纪元、未暴露项和状态额度。

        :param inputs (ResolutionInputs): 已锁定原始输入与目标
        :param result (SkillTreeManifest): 完整人工选择结果
        :param choices (list[SkillResolutionChoice]): 用户明确选择
        :return ResolvedTargets: 预览通过的实际写入目标
        """
        context = self.context.publication
        writable = {
            branch.item.entry_name for branch in inputs.branches if branch.checkpoint is not None
        }
        for name in inputs.members.keys() - writable:
            if subtree_entries(inputs.current, name) != subtree_entries(result, name):
                raise SkillContentError(
                    "STATE_SCOPE_MISMATCH", "resolution changes an unexposed source"
                )
        branches = []
        for branch in inputs.branches:
            name = branch.item.entry_name
            if branch.checkpoint is None:
                if subtree_entries(inputs.current, name) != subtree_entries(result, name):
                    raise SkillContentError(
                        "STATE_SCOPE_MISMATCH", "resolution revives an unavailable source"
                    )
                branches.append(branch)
                continue
            assert branch.checkpoint.tree_digest is not None
            original = await context.content.read_tree(
                inputs.publication.user_id, "state", branch.checkpoint.tree_digest
            )
            changed = subtree_entries(original, name) != subtree_entries(result, name)
            if changed:
                revision = branch.state.base_revision_id or branch.state.local_revision_id
                if branch.state.epoch != branch.item.state_epoch or str(
                    revision
                ) != branch.item.resolution_json.get("revision_id"):
                    raise SkillContentError(
                        "STATE_EPOCH_CHANGED", "resolution cannot revive pre-reset state"
                    )
                if await context.source_invalid(branch.state) is not None:
                    raise SkillContentError(
                        "SOURCE_REMOVED", "resolution target source is no longer active"
                    )
            branches.append(replace(branch, changed=changed))
        valid_names = await valid_candidate_names(self.store, inputs.publication.user_id, result)
        occupied = {
            item.name
            for item in await context.library.list_installations(inputs.publication.user_id)
        }
        occupied |= await context.repository.active_local_names(
            inputs.publication.user_id, inputs.publication.account_id
        )
        candidates = []
        for item in inputs.candidates:
            if item.name in occupied:
                retained = any(
                    choice.use == "current"
                    and (choice.whole or item.name in choice.unit or choice.path == item.name)
                    for choice in choices
                )
                if not retained or subtree_entries(inputs.current, item.name) != subtree_entries(
                    result, item.name
                ):
                    raise SkillContentError(
                        "SKILL_SOURCE_CONFLICT",
                        "incoming source cannot take over an existing identity",
                    )
            elif item.name in valid_names:
                if item.status != "staged":
                    raise SkillContentError(
                        "SOURCE_CHANGED", "candidate identity is no longer staged"
                    )
                candidates.append(item)
        new_names = valid_names - inputs.names
        if new_names & occupied:
            raise SkillContentError(
                "SKILL_SOURCE_CONFLICT", "custom result collides with an existing source"
            )
        names = (
            set(inputs.members)
            | {branch.item.entry_name for branch in branches}
            | {item.name for item in candidates}
            | new_names
        )
        validate_finalization_limits(result, names, self.policy)
        await self.store.verify_manifest(inputs.publication.user_id, result)
        return ResolvedTargets(branches, candidates, new_names)

    async def publish(
        self,
        inputs: ResolutionInputs,
        result: SkillTreeManifest,
        targets: ResolvedTargets,
        revision: int,
    ) -> SkillCheckpoint:
        """
        完整内容、人工新增来源和所有 head 与原计划同时提交。

        :param inputs (ResolutionInputs): 不变的发布前置条件
        :param result (SkillTreeManifest): 完整验证结果
        :param targets (ResolvedTargets): 实际写入集合
        :param revision (int): 本次计划版本
        :return SkillCheckpoint: 已发布的完整目录
        """
        context = self.context.publication
        assert inputs.directory is not None
        user_id = inputs.publication.user_id
        upload = await context.content.begin(
            user_id, f"resolution:{inputs.publication.id}:{revision}", result, "account_directory"
        )
        tree = await context.content.complete(user_id, upload.id)
        candidates = list(targets.candidates)
        if targets.new_names:
            source = SkillCheckpoint(
                id=uuid4(),
                user_id=user_id,
                account_id=inputs.publication.account_id,
                scope="directory",
                directory_epoch=inputs.directory.epoch,
                tree_digest=tree.digest,
                content_digest=tree.digest,
                source_session_reference_id=inputs.snapshot.session_reference_id,
            )
            context.runtime.add(source)
            await context.runtime.flush()
            for name in sorted(targets.new_names):
                candidates.append(
                    await self.candidates.register(user_id, source.account_id, source.id, name)
                )
        checkpoint = await PublicationApply(context, self.store).publish(
            inputs.snapshot,
            inputs.directory,
            result,
            tree.digest,
            targets.branches,
            dict(inputs.members),
            candidates,
        )
        inputs.publication.status = "published"
        inputs.publication.result_checkpoint_id = checkpoint.id
        inputs.receipt.status = "published"
        await context.runtime.flush()
        return checkpoint
