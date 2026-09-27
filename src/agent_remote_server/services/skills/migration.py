"""
执行同源跨版本增量迁移并在完整发布后保存精确成功基线。
"""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.repositories.skill_preparation import SkillPreparationRepository
from agent_remote_server.repositories.skill_preparation_lifecycle import (
    SkillPreparationLifecycleRepository,
)
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_migration import (
    SkillMigrationPrecondition,
    SkillMigrationReceipt,
    SkillMigrationRequest,
    SkillMigrationView,
)
from agent_remote_server.services.skills.branch_publication import SkillBranchPublisher
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.migration_plan import IncrementalMigrationPlanner
from agent_remote_server.services.skills.migration_selection import MigrationSelection
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy
from agent_remote_server.skill_manager.tree_diff import manifest_difference


class SkillMigrationService:
    """
    受理回执、成功序号和目标 head 位于同一保存点，不更新有效规则或使用顺序。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        建立只读选择、三侧规划与共享原子发布器。

        :param session (AsyncSession): 外层请求事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 内容额度策略
        """
        self._session = session
        self.queries = SkillStateQueryService(session, store, policy)
        self.repository = SkillPreparationRepository(session)
        self.selection = MigrationSelection(self.queries, self.repository)
        self._planner = IncrementalMigrationPlanner(MigrationContent(self.queries, store), policy)
        self._publisher = SkillBranchPublisher(
            self.queries, SkillPublicationRepository(session), store
        )

    async def execute(self, user_id: UUID, request: SkillMigrationRequest) -> SkillMigrationView:
        """
        对精确源和目标状态进行条件提交，旧幂等键重放先于最新状态校验。

        :param user_id (UUID): 已认证所有者
        :param request (SkillMigrationRequest): 完整预览及明确版本方向
        :return SkillMigrationView: 完整成功或持久化冲突结果
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
                    if previous.request_digest != digest or previous.mode != "incremental":
                        raise SkillContentError(
                            "IDEMPOTENCY_CONFLICT", "key belongs to another migration command"
                        )
                    return SkillMigrationView.model_validate(previous.response_json)
            state = await self.selection.current(user_id, request.selector)
            if state != request.expected:
                raise SkillContentError(
                    "STATE_PRECONDITION_CHANGED",
                    "migration source, target or baseline changed; preview again",
                    details={"current": state.model_dump(mode="json")},
                )
            if state.last_sequence >= 2**63 - 1:
                raise SkillContentError("LIMIT_EXCEEDED", "migration sequence exhausted")
            plan = await self._planner.prepare(user_id, state)
            operation_id = None if request.dry_run else uuid4()
            sequence = None
            item_id = None
            directory_id = None
            branch = None
            if operation_id is not None:
                branch = await self._target(user_id, state)
                for label, tree in (
                    ("base", plan.base),
                    ("current", plan.current),
                    ("incoming", plan.incoming),
                ):
                    upload = await self.queries.content.begin(
                        user_id, f"migration:{operation_id}:{label}", tree, "account_directory"
                    )
                    await self.queries.content.complete(user_id, upload.id)
                if plan.publication is not None:
                    if plan.unchanged:
                        item_id, directory_id = (
                            branch.head_checkpoint_id,
                            state.directory_checkpoint_id,
                        )
                    else:
                        item_id, directory_id = await self._publisher.publish(
                            branch, plan.publication, f"migration:{operation_id}"
                        )
                    sequence = state.last_sequence + 1
            result_tree = plan.publication.result if plan.publication else None
            result = SkillMigrationView(
                operation_id=operation_id,
                status="ready" if result_tree is not None else "conflicted",
                before=state,
                base_source="last_migrated"
                if state.last_migrated_checkpoint_id
                else "old_original",
                current_source="target_published"
                if state.target.checkpoint_id
                else "target_original",
                base_digest=manifest_digest(plan.base),
                current_digest=manifest_digest(plan.current),
                incoming_digest=manifest_digest(plan.incoming),
                result_tree_digest=manifest_digest(result_tree)
                if result_tree is not None
                else None,
                result_checkpoint_id=item_id,
                result_directory_id=directory_id,
                migration_sequence=sequence,
                conflicts=plan.conflicts,
                changes=[]
                if plan.unchanged
                else manifest_difference(plan.current, result_tree, state.name)
                if result_tree is not None
                else None,
                directory_changes=manifest_difference(plan.directory, result_tree, None)
                if result_tree is not None
                else None,
            )
            if operation_id is not None:
                assert branch is not None
                self.queries.runtime.add(
                    SkillBranchPreparation(
                        id=operation_id,
                        user_id=user_id,
                        account_id=state.account_id,
                        installation_id=state.skill_id,
                        installation_epoch=state.installation_epoch,
                        idempotency_key=request.idempotency_key,
                        request_digest=digest,
                        target_state_id=branch.id,
                        target_epoch=branch.epoch,
                        source_state_id=state.source.state_id,
                        source_epoch=state.source.state_epoch,
                        source_checkpoint_id=state.source.checkpoint_id,
                        base_checkpoint_id=state.last_migrated_checkpoint_id,
                        current_checkpoint_id=state.target.checkpoint_id,
                        directory_epoch=state.directory_epoch,
                        library_generation=state.library_generation,
                        directory_checkpoint_id=state.directory_checkpoint_id,
                        result_checkpoint_id=item_id,
                        result_directory_id=directory_id,
                        base_digest=result.base_digest,
                        current_digest=result.current_digest,
                        incoming_digest=result.incoming_digest,
                        mode="incremental",
                        status=result.status,
                        migration_sequence=sequence,
                        response_json=result.model_dump(mode="json"),
                    )
                )
                await self.queries.runtime.flush()
                await SkillPreparationLifecycleRepository(self._session).supersede_success(
                    user_id, operation_id
                )
            return result

    async def _target(self, user_id: UUID, state: SkillMigrationPrecondition) -> AccountSkillState:
        """
        精确目标可在本次事务创建，但绝不借用当前规则选择的另一版本。

        :param user_id (UUID): 所有者
        :param state (SkillMigrationPrecondition): 已核对完整状态
        :return AccountSkillState: 本次明确目标分支
        """
        branch = await self.queries.runtime.branch(
            user_id,
            state.account_id,
            state.skill_id,
            state.installation_epoch,
            state.target.revision_id,
        )
        if branch is None:
            if state.target.state_id is not None:
                raise SkillContentError("HEAD_CHANGED", "migration target disappeared")
            branch = AccountSkillState(
                id=uuid4(),
                user_id=user_id,
                account_id=state.account_id,
                installation_id=state.skill_id,
                installation_epoch=state.installation_epoch,
                base_revision_id=state.target.revision_id,
                epoch=1,
                expired=False,
            )
            self.queries.runtime.add(branch)
            await self.queries.runtime.flush()
        elif (branch.id, branch.epoch, branch.head_checkpoint_id, branch.expired) != (
            state.target.state_id,
            state.target.state_epoch,
            state.target.checkpoint_id,
            state.target.expired,
        ):
            raise SkillContentError("HEAD_CHANGED", "migration target changed after preview")
        return branch

    async def receipt(self, user_id: UUID, key: str) -> SkillMigrationReceipt:
        """
        读取原始受理和当前失效状态，不执行历史请求。

        :param user_id (UUID): 已认证所有者
        :param key (str): 原始幂等键
        :return SkillMigrationReceipt: 不可变结果和独立失效状态
        """
        row = await self.repository.receipt(user_id, key)
        return self._receipt(row)

    async def receipt_by_id(self, user_id: UUID, operation_id: UUID) -> SkillMigrationReceipt:
        """
        通过原始操作身份查询独立增量回执，不执行或重解释请求。

        :param user_id (UUID): 已认证所有者
        :param operation_id (UUID): 原始操作身份
        :return SkillMigrationReceipt: 原始结果与当前失效信息
        """
        return self._receipt(await self.repository.receipt_by_id(user_id, operation_id))

    @staticmethod
    def _receipt(row: SkillBranchPreparation | None) -> SkillMigrationReceipt:
        """
        两种查询使用相同类型检查并保留不可变结果。

        :param row (SkillBranchPreparation | None): 已按所有者过滤的记录
        :return SkillMigrationReceipt: 增量迁移回执
        """
        if row is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "migration operation not found")
        if row.mode != "incremental":
            raise SkillContentError(
                "OPERATION_KIND_MISMATCH", "use the preparation receipt endpoint"
            )
        return SkillMigrationReceipt.model_validate(
            {
                "result": row.response_json,
                "current_status": row.status,
                "replacement_id": row.replacement_id,
                "superseded_reason": row.superseded_reason,
            }
        )
