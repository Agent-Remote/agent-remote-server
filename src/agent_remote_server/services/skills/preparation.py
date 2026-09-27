"""
协调独立版本准备预览、完整迁移受理与不可变幂等结果。
"""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_preparation import SkillPreparationRepository
from agent_remote_server.repositories.skill_preparation_lifecycle import (
    SkillPreparationLifecycleRepository,
)
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_preparation import (
    SkillPreparationReceipt,
    SkillPreparationRequest,
    SkillPreparationView,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.preparation_apply import BranchPreparationApply
from agent_remote_server.services.skills.preparation_plan import BranchPreparationPlanner
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.services.skills.state_selection import StateSelection
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillPreparationService:
    """
    冲突是可提交的正常结果，调用者必须先提交它再决定是否拒绝会话准入。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        为同事务查询、规划与发布建立明确依赖。

        :param session (AsyncSession): 请求事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 部署配额
        """
        self._session = session
        self.queries = SkillStateQueryService(session, store, policy)
        self.selection = StateSelection(self.queries, SkillLocalRepository(session))
        self.repository = SkillPreparationRepository(session)
        self._planner = BranchPreparationPlanner(
            self.queries, self.repository, MigrationContent(self.queries, store), policy
        )
        self._apply = BranchPreparationApply(
            self.queries, SkillPublicationRepository(session), store
        )

    async def execute(
        self, user_id: UUID, request: SkillPreparationRequest
    ) -> SkillPreparationView:
        """
        重放早于今天的规则检查，保存点保证迁移最后一步失败也无部分发布。

        :param user_id (UUID): 已认证所有者
        :param request (SkillPreparationRequest): 精确目标和幂等键
        :return SkillPreparationView: 完整可用结果或已保留的迁移冲突
        """
        digest = hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        async with retention_mutation(self._session, user_id, read_only=request.dry_run):
            if request.dry_run:
                await self.queries.library.read_library(user_id)
            else:
                await self.queries.library.lock_library(user_id)
                previous = await self.repository.receipt(user_id, request.idempotency_key)
                if previous is not None:
                    if previous.request_digest != digest:
                        raise SkillContentError(
                            "IDEMPOTENCY_CONFLICT", "key belongs to another branch preparation"
                        )
                    return SkillPreparationView.model_validate(previous.response_json)
            selection = await self.selection.current(user_id, request.selector)
            if selection.precondition != request.expected:
                raise SkillContentError(
                    "STATE_PRECONDITION_CHANGED",
                    "selected state changed; preview again",
                    details={"current": selection.precondition.model_dump(mode="json")},
                )
            plan = await self._planner.prepare(user_id, selection)
            target = selection.precondition.targets[0]
            operation_id = None if request.dry_run else uuid4()
            target_id = target.state_id
            result_checkpoint = None
            result_directory = None
            sequence = None
            if operation_id is not None:
                branch = await self._apply.branch(user_id, selection)
                target_id = branch.id
                if plan.mode == "forward" and plan.result is not None:
                    assert plan.source is not None
                    assert selection.precondition.directory_epoch is not None
                    last = await self.repository.latest_migration(
                        plan.source, branch, selection.precondition.directory_epoch
                    )
                    sequence = (last.migration_sequence or 0) + 1 if last else 1
                    if sequence > 2**63 - 1:
                        raise SkillContentError("LIMIT_EXCEEDED", "migration sequence exhausted")
                for label, tree in (
                    ("base", plan.base),
                    ("current", plan.current),
                    ("incoming", plan.incoming),
                ):
                    upload = await self.queries.content.begin(
                        user_id, f"prepare:{operation_id}:{label}", tree, "account_directory"
                    )
                    await self.queries.content.complete(user_id, upload.id)
                if plan.result is not None:
                    result_checkpoint, result_directory = await self._apply.publish(
                        branch, selection, plan, operation_id
                    )
            result = SkillPreparationView(
                operation_id=operation_id,
                migration_sequence=sequence,
                status="ready" if plan.result is not None else "conflicted",
                mode=plan.mode,
                before=selection.precondition,
                source_revision_id=plan.source.base_revision_id if plan.source else None,
                source_checkpoint_id=plan.source_checkpoint.id if plan.source_checkpoint else None,
                source_epoch=plan.source.epoch if plan.source else None,
                target_state_id=target_id,
                result_checkpoint_id=result_checkpoint,
                result_directory_id=result_directory,
                result_tree_digest=manifest_digest(plan.result)
                if plan.result is not None
                else None,
                base_source="existing_target"
                if plan.mode == "resume"
                else "target_original"
                if plan.mode == "initial"
                else "old_original",
                current_source="existing_target"
                if plan.mode == "resume"
                else "target_original"
                if plan.mode == "initial"
                else "new_original",
                incoming_source="existing_target"
                if plan.mode == "resume"
                else "target_original"
                if plan.mode == "initial"
                else "old_published",
                base_digest=manifest_digest(plan.base),
                current_digest=manifest_digest(plan.current),
                incoming_digest=manifest_digest(plan.incoming),
                conflicts=plan.conflicts,
                warnings=("newer_state_not_migrated",) if plan.mode == "older" else (),
            )
            if operation_id is not None:
                assert target_id is not None
                assert selection.precondition.directory_epoch is not None
                self.queries.runtime.add(
                    SkillBranchPreparation(
                        id=operation_id,
                        user_id=user_id,
                        account_id=request.selector.account_id,
                        installation_id=target.skill_id,
                        installation_epoch=target.installation_epoch,
                        idempotency_key=request.idempotency_key,
                        request_digest=digest,
                        target_state_id=target_id,
                        target_epoch=target.state_epoch or 1,
                        source_state_id=plan.source.id if plan.source else None,
                        source_epoch=result.source_epoch,
                        source_checkpoint_id=result.source_checkpoint_id,
                        directory_epoch=selection.precondition.directory_epoch,
                        library_generation=selection.precondition.library_generation,
                        directory_checkpoint_id=plan.directory.id,
                        result_checkpoint_id=result_checkpoint,
                        result_directory_id=result_directory,
                        base_digest=result.base_digest,
                        current_digest=result.current_digest,
                        incoming_digest=result.incoming_digest,
                        migration_sequence=sequence,
                        mode=plan.mode,
                        status=result.status,
                        response_json=result.model_dump(mode="json"),
                    )
                )
                await self.queries.runtime.flush()
                await SkillPreparationLifecycleRepository(self._session).supersede_success(
                    user_id, operation_id
                )
            return result

    async def receipt(self, user_id: UUID, key: str) -> SkillPreparationReceipt:
        """
        失效状态与原始受理结果分别返回，查询绝不重放迁移动作。

        :param user_id (UUID): 当前用户
        :param key (str): 原始幂等键
        :return SkillPreparationReceipt: 原始结果和当前失效状态
        """
        row = await self.repository.receipt(user_id, key)
        if row is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "branch preparation not found")
        if row.mode == "incremental":
            raise SkillContentError("OPERATION_KIND_MISMATCH", "use the migration receipt endpoint")
        return SkillPreparationReceipt.model_validate(
            {
                "result": row.response_json,
                "current_status": row.status,
                "replacement_id": row.replacement_id,
                "superseded_reason": row.superseded_reason,
            }
        )
