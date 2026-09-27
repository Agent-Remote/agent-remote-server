"""
为过期迁移建立唯一替代比较，纪元或身份失效只终止旧计划而不复活数据。
"""

import hashlib
from dataclasses import dataclass
from uuid import UUID, uuid4

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_preparation_lifecycle import (
    SkillPreparationLifecycleRepository,
)
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_migration_conflicts import SkillMigrationConflictView
from agent_remote_server.schemas.skill_preparation import SkillPreparationView
from agent_remote_server.schemas.skill_state_commands import (
    SkillStatePrecondition,
    SkillStateSelector,
)
from agent_remote_server.services.skills.branch_publication import SkillBranchPublisher
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.migration_plan import IncrementalMigrationPlan
from agent_remote_server.services.skills.migration_recompute_plan import MigrationRecomputePlanner
from agent_remote_server.services.skills.migration_resolution_plan import MigrationResolutionPlanner
from agent_remote_server.services.skills.publication_context import PublicationContext
from agent_remote_server.services.skills.state_selection import StateSelection
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy
from agent_remote_server.skill_manager.tree_diff import manifest_difference


@dataclass(frozen=True)
class MigrationStaleness:
    """
    将可重算竞争和不能复活的失效分开，既有替代关系优先返回。
    """

    reasons: tuple[str, ...]
    recompute: bool
    replacement_id: UUID | None = None


@dataclass
class MigrationRecomputation:
    """
    调用方持有用户写锁及保存点，本层从不接收任何解决选择。
    """

    planner: MigrationResolutionPlanner
    branches: SkillPublicationRepository
    local: SkillLocalRepository
    lifecycle: SkillPreparationLifecycleRepository
    store: PrivateObjectStore
    policy: SkillStoragePolicy

    async def inspect(
        self, row: SkillBranchPreparation, info: SkillMigrationConflictView
    ) -> MigrationStaleness | None:
        """
        同纪元来源新 head 不使保存输入失效，真实纪元和稳定身份变化必须终止。

        :param row (SkillBranchPreparation): 原始迁移
        :param info (SkillMigrationConflictView): 本事务原输入及当前状态
        :return MigrationStaleness | None: 失效或重算分类，无变化为空
        """
        invalidated = (
            row.status == "superseded"
            and row.replacement_id is None
            and row.superseded_reason in {"state_reset", "state_restore"}
        )
        terminal = MigrationStaleness(
            (row.superseded_reason or "superseded",), False, row.replacement_id
        )
        if row.status == "superseded" and not invalidated:
            return terminal
        if row.status != "conflicted" and not invalidated:
            raise SkillContentError("CONFLICT_NOT_ACTIVE", "migration is no longer active")
        current = await self._current(row, info)
        if invalidated:
            if current is not None and current.recompute:
                assert row.superseded_reason is not None
                return MigrationStaleness((*current.reasons, row.superseded_reason), True)
            return terminal
        return current

    async def _current(
        self, row: SkillBranchPreparation, info: SkillMigrationConflictView
    ) -> MigrationStaleness | None:
        """
        依据真实目标与关联纪元判断，不把账户级粗粒度取消当成所有分支都已重置。

        :param row (SkillBranchPreparation): 保存的原始尝试
        :param info (SkillMigrationConflictView): 本事务实时诊断
        :return MigrationStaleness | None: 真实变化类别或没有变化
        """
        reasons = tuple(
            reason for reason in info.live.recomputation_reasons if reason != "superseded"
        )
        if not reasons:
            return None
        if "migration_baseline_changed" in reasons:
            return MigrationStaleness(reasons, False, info.live.last_migration_id)
        if set(reasons) - {
            "directory_head_changed",
            "target_head_changed",
            "library_generation_changed",
        }:
            return MigrationStaleness(reasons, False)
        queries = self.planner.conflicts.queries
        if row.mode != "incremental":
            selection = await StateSelection(queries, self.local).current(
                row.user_id,
                SkillStateSelector(
                    account_id=row.account_id, scope="item", skill=str(row.installation_id)
                ),
            )
            target = selection.precondition.targets[0]
            if not target.rule.included or target.revision_id != info.live.target.revision_id:
                return MigrationStaleness((*reasons, "effective_target_changed"), False)
        if (
            await queries.repository.local_source(row.user_id, row.account_id, info.name)
            is not None
        ):
            return MigrationStaleness((*reasons, "target_source_changed"), False)
        related_reason = await self._related_reason(row, info)
        if related_reason is not None:
            return MigrationStaleness((*reasons, related_reason), False)
        return MigrationStaleness(reasons, True)

    async def _related_reason(
        self, row: SkillBranchPreparation, info: SkillMigrationConflictView
    ) -> str | None:
        """
        真实改动成员的纪元失效不能被掩盖，原样关联成员不算本次写入。

        :param row (SkillBranchPreparation): 原始尝试
        :param info (SkillMigrationConflictView): 保存输入及实时身份
        :return str | None: 关联成员不可重算的原因
        """
        inputs = await self.planner.inputs(info, row.user_id)
        changed = await self.planner.related_writes(row, inputs)
        queries = self.planner.conflicts.queries
        directory = await queries.require(row.user_id, row.directory_checkpoint_id)
        assert info.live.directory_checkpoint_id is not None
        current_directory = await queries.require(row.user_id, info.live.directory_checkpoint_id)
        current_members = {
            member.entry_name: member for member in await queries.runtime.members(current_directory)
        }
        for member in await queries.runtime.members(directory):
            if member.entry_name not in changed:
                continue
            current_member = current_members.get(member.entry_name)
            if current_member is None or current_member.state_id != member.state_id:
                return "related_source_changed"
            checkpoint = await queries.require(row.user_id, member.checkpoint_id)
            branch = await self.branches.branch(row.user_id, row.account_id, member.state_id)
            context = PublicationContext(
                queries.library, self.branches, queries.runtime, queries.content
            )
            if await context.source_invalid(branch) is not None:
                return "related_source_changed"
            if checkpoint.state_epoch is None:
                return "related_provenance_unavailable"
            if checkpoint.state_epoch != branch.epoch or branch.expired:
                return "related_epoch_changed"
        return None

    async def execute(
        self,
        row: SkillBranchPreparation,
        info: SkillMigrationConflictView,
        stale: MigrationStaleness,
    ) -> UUID | None:
        """
        在调用方保存点中创建全新空计划比较或记录终止，任何失败由外层整体回滚。

        :param row (SkillBranchPreparation): 原始尝试
        :param info (SkillMigrationConflictView): 真实实时诊断
        :param stale (MigrationStaleness): 已核对的失效分类
        :return UUID | None: 新比较或已存在的替代记录，没有可继续记录时为空
        """
        if row.status == "superseded" and not stale.recompute:
            return row.replacement_id
        replacement_id = stale.replacement_id
        if stale.recompute:
            replacement_id = await self._replace(row, info)
        if not await self.planner.conflicts.preparations.replace_attempt(
            row, stale.reasons[-1], replacement_id
        ):
            raise SkillContentError("CONFLICT_NOT_ACTIVE", "migration changed during recomputation")
        return replacement_id

    async def _replace(self, row: SkillBranchPreparation, info: SkillMigrationConflictView) -> UUID:
        """
        新记录保留原来源检查点和模式，完整无冲突结果可自动提交但不采用旧选择。

        :param row (SkillBranchPreparation): 已验证旧迁移
        :param info (SkillMigrationConflictView): 当前同纪元目标
        :return UUID: 唯一新比较身份
        """
        queries = self.planner.conflicts.queries
        target = await self.planner.conflicts.repository.branch(row, row.target_state_id)
        selection = (
            await StateSelection(queries, self.local).current(
                row.user_id,
                SkillStateSelector(
                    account_id=row.account_id, scope="item", skill=str(row.installation_id)
                ),
            )
            if row.mode != "incremental"
            else None
        )
        plan = await MigrationRecomputePlanner(
            MigrationContent(queries, self.store), self.policy
        ).prepare(row, target, info.name)
        identity = uuid4()
        for label, tree in (
            ("base", plan.base),
            ("current", plan.current),
            ("incoming", plan.incoming),
        ):
            upload = await queries.content.begin(
                row.user_id, f"recompute:{identity}:{label}", tree, "account_directory"
            )
            await queries.content.complete(row.user_id, upload.id)
        checkpoint_id = directory_id = sequence = None
        if plan.publication is not None:
            if row.mode in {"forward", "incremental"}:
                assert row.source_state_id is not None
                source = await self.planner.conflicts.repository.branch(row, row.source_state_id)
                last = await self.planner.conflicts.preparations.latest_migration(
                    source, target, row.directory_epoch
                )
                sequence = (last.migration_sequence or 0) + 1 if last else 1
                if sequence > 2**63 - 1:
                    raise SkillContentError("LIMIT_EXCEEDED", "migration sequence exhausted")
            checkpoint_id, directory_id = await SkillBranchPublisher(
                queries, self.branches, self.store
            ).publish(target, plan.publication, f"recompute:{identity}")
        response = _response(
            info,
            target,
            plan,
            identity,
            checkpoint_id,
            directory_id,
            sequence,
            selection.precondition if selection is not None else None,
        )
        assert info.live.directory_checkpoint_id is not None
        replacement = SkillBranchPreparation(
            id=identity,
            user_id=row.user_id,
            account_id=row.account_id,
            installation_id=row.installation_id,
            installation_epoch=row.installation_epoch,
            idempotency_key=f"recompute:{identity}",
            request_digest=hashlib.sha256(str(row.id).encode()).hexdigest(),
            recomputed_from_id=row.id,
            target_state_id=row.target_state_id,
            target_epoch=row.target_epoch,
            source_state_id=row.source_state_id,
            source_epoch=row.source_epoch,
            source_checkpoint_id=row.source_checkpoint_id,
            base_checkpoint_id=row.base_checkpoint_id,
            current_checkpoint_id=info.live.target.checkpoint_id,
            directory_epoch=row.directory_epoch,
            library_generation=info.live.library_generation,
            directory_checkpoint_id=info.live.directory_checkpoint_id,
            result_checkpoint_id=checkpoint_id,
            result_directory_id=directory_id,
            base_digest=manifest_digest(plan.base),
            current_digest=manifest_digest(plan.current),
            incoming_digest=manifest_digest(plan.incoming),
            migration_sequence=sequence,
            mode=row.mode,
            status=response.status,
            response_json=response.model_dump(mode="json"),
        )
        queries.runtime.add(replacement)
        await queries.runtime.flush()
        await self.lifecycle.supersede_success(row.user_id, identity)
        return identity


def _response(
    info: SkillMigrationConflictView,
    target: AccountSkillState,
    plan: IncrementalMigrationPlan,
    identity: UUID,
    checkpoint_id: UUID | None,
    directory_id: UUID | None,
    sequence: int | None,
    precondition: SkillStatePrecondition | None,
) -> SkillMigrationView | SkillPreparationView:
    """
    重算响应使用当前目标身份，原尝试 JSON 不被复用为可变状态。

    :param info (SkillMigrationConflictView): 原响应及当前身份
    :param target (AccountSkillState): 新比较目标
    :param plan (IncrementalMigrationPlan): 已验证比较
    :param identity (UUID): 新尝试身份
    :param checkpoint_id (UUID | None): 已发布目标或空
    :param directory_id (UUID | None): 已发布目录或空
    :param sequence (int | None): 新成功序号或空
    :param precondition (SkillStatePrecondition | None): 准备模式的实际发布前选择，显式迁移为空
    :return SkillMigrationView | SkillPreparationView: 新尝试不可变原始响应
    """
    original = info.original
    result = plan.publication.result if plan.publication else None
    updates: dict[str, object] = {
        "operation_id": identity,
        "status": "ready" if result is not None else "conflicted",
        "base_digest": manifest_digest(plan.base),
        "current_digest": manifest_digest(plan.current),
        "incoming_digest": manifest_digest(plan.incoming),
        "conflicts": plan.conflicts,
        "result_tree_digest": manifest_digest(result) if result is not None else None,
        "result_checkpoint_id": checkpoint_id,
        "result_directory_id": directory_id,
        "migration_sequence": sequence,
    }
    if isinstance(original, SkillMigrationView):
        current = original.before.target.model_copy(
            update={
                "state_id": target.id,
                "state_epoch": target.epoch,
                "checkpoint_id": info.live.target.checkpoint_id,
                "expired": False,
            }
        )
        before = original.before.model_copy(
            update={
                "library_generation": info.live.library_generation,
                "directory_checkpoint_id": info.live.directory_checkpoint_id,
                "target": current,
            }
        )
        updates.update(
            before=before,
            current_source="target_published" if current.checkpoint_id else "target_original",
            changes=manifest_difference(plan.current, result, info.name) if result else None,
            directory_changes=manifest_difference(plan.directory, result, None) if result else None,
        )
        return SkillMigrationView.model_validate({**original.model_dump(), **updates})
    assert precondition is not None
    updates.update(
        before=precondition,
        target_state_id=target.id,
        current_source="target_published"
        if info.live.target.checkpoint_id
        else original.current_source,
    )
    return SkillPreparationView.model_validate({**original.model_dump(), **updates})
