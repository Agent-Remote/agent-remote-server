"""
在同一保存点内解决迁移计划并发布完整关联目录，原输入与原受理响应保持不变。
"""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_migration_resolution import SkillMigrationResolutionOperation
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_preparation_lifecycle import (
    SkillPreparationLifecycleRepository,
)
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_migration_resolution import (
    SkillMigrationResolutionReceipt,
    SkillMigrationResolutionView,
)
from agent_remote_server.schemas.skill_resolution import SkillResolutionRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_recompute import MigrationRecomputation
from agent_remote_server.services.skills.migration_resolution_apply import MigrationResolutionApply
from agent_remote_server.services.skills.migration_resolution_choices import choice_row, choice_spec
from agent_remote_server.services.skills.migration_resolution_plan import MigrationResolutionPlanner
from agent_remote_server.services.skills.migration_resolution_views import (
    resolution_details,
    stale_resolution_view,
)
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillMigrationResolutionService:
    """
    每个选择以版本 CAS 保存，只有完整结果才能推进全部分支及成功迁移基线。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        共享用户锁、候选授权、全部发布引用及不可变操作仓储。

        :param session (AsyncSession): 外层请求事务
        :param store (PrivateObjectStore): 私有完整内容卷
        :param policy (SkillStoragePolicy): 内容与状态额度
        """
        self._session = session
        self.planner = MigrationResolutionPlanner(session, store, policy)
        self.repository = self.planner.repository
        self.queries = self.planner.conflicts.queries
        self._apply = MigrationResolutionApply(
            self.queries, SkillPublicationRepository(session), SkillLocalRepository(session), store
        )
        self._recompute = MigrationRecomputation(
            self.planner,
            self._apply.repository,
            self._apply.local,
            SkillPreparationLifecycleRepository(session),
            store,
            policy,
        )

    async def execute(
        self, user_id: UUID, migration_id: UUID, request: SkillResolutionRequest
    ) -> SkillMigrationResolutionView:
        """
        重放先于当前状态校验，内容、计划、所有 head、成功序号和回执一起提交。

        :param user_id (UUID): 认证所有者
        :param migration_id (UUID): 原始迁移冲突身份
        :param request (SkillResolutionRequest): 单次明确选择和预期版本
        :return SkillMigrationResolutionView: 只读预览、待补齐计划或完整已发布结果
        """
        digest = hashlib.sha256(
            json.dumps(
                {
                    "kind": "migration_resolution",
                    "migration_id": str(migration_id),
                    "request": request.model_dump(mode="json"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with retention_mutation(self._session, user_id, read_only=request.dry_run):
            if request.dry_run:
                await self.queries.library.read_library(user_id)
            else:
                await self.queries.library.lock_library(user_id)
                previous = await self.repository.operation(user_id, request.idempotency_key)
                if previous is not None:
                    if previous.request_digest != digest:
                        raise SkillContentError(
                            "IDEMPOTENCY_CONFLICT", "key belongs to another resolution"
                        )
                    return _original(previous)
            migration = await self.planner.conflicts.require(user_id, migration_id)
            if migration.content_retired_at is not None:
                raise SkillContentError("STATE_EXPIRED", "migration comparison has expired")
            plan = await self.repository.plan(migration)
            revision = plan.revision if plan else 0
            if revision != request.expected_revision:
                raise SkillContentError(
                    "PLAN_REVISION_CONFLICT",
                    "migration resolution plan changed",
                    details={"expected": request.expected_revision, "current": revision},
                )
            info = await self.planner.conflicts.info(user_id, migration_id)
            stale = await self._recompute.inspect(migration, info)
            if stale is not None:
                operation_id = None if request.dry_run else uuid4()
                replacement = (
                    stale.replacement_id
                    if request.dry_run
                    else await self._recompute.execute(migration, info, stale)
                )
                view = stale_resolution_view(
                    info,
                    [choice_spec(row) for row in await self.repository.choices(migration)],
                    revision,
                    operation_id,
                    stale.reasons,
                    stale.recompute,
                    replacement,
                )
                if operation_id is not None:
                    await self.repository.save_operation(
                        SkillMigrationResolutionOperation(
                            id=operation_id,
                            user_id=user_id,
                            account_id=migration.account_id,
                            migration_id=migration_id,
                            idempotency_key=request.idempotency_key,
                            request_digest=digest,
                            plan_revision=revision,
                            response_json=view.model_dump(mode="json"),
                        )
                    )
                return view
            calculation = await self.planner.calculate(
                user_id,
                migration_id,
                [choice_spec(row) for row in await self.repository.choices(migration)],
                replacement=request.choice,
            )
            publication = (
                await self._apply.prepare(migration, calculation)
                if calculation.result.merged is not None
                else None
            )
            operation_id = None if request.dry_run else uuid4()
            checkpoint_id = directory_id = sequence = None
            affected = publication.affected if publication else []
            next_sequence = None
            if publication is not None and migration.source_state_id is not None:
                source = await self.planner.conflicts.repository.branch(
                    migration, migration.source_state_id
                )
                if source.epoch != migration.source_epoch or source.expired:
                    raise SkillContentError(
                        "STATE_EPOCH_CHANGED", "source changed before resolution publication"
                    )
            if publication is not None and migration.mode in {"forward", "incremental"}:
                assert migration.source_state_id is not None
                last = await self.planner.conflicts.preparations.latest_migration(
                    source, publication.target, migration.directory_epoch
                )
                next_sequence = (last.migration_sequence or 0) + 1 if last else 1
                if next_sequence > 2**63 - 1:
                    raise SkillContentError("LIMIT_EXCEEDED", "migration sequence exhausted")
            if operation_id is not None:
                if not await self.repository.replace_choices(
                    migration,
                    revision,
                    [choice_row(migration, choice) for choice in calculation.choices],
                ):
                    raise SkillContentError(
                        "PLAN_REVISION_CONFLICT", "plan changed during resolution"
                    )
                revision += 1
                if publication is not None:
                    sequence = next_sequence
                    items, directory_id = await self._apply.publish(publication, operation_id)
                    checkpoint_id = items[calculation.inputs.name]
                    if not await self.planner.conflicts.preparations.resolve(
                        migration, checkpoint_id, directory_id, sequence
                    ):
                        raise SkillContentError(
                            "CONFLICT_NOT_ACTIVE", "migration changed during publication"
                        )
                    affected = [
                        item.model_copy(update={"result_checkpoint_id": items[item.name]})
                        for item in affected
                    ]
                    await SkillPreparationLifecycleRepository(self._session).supersede_success(
                        user_id, migration_id
                    )
            view = SkillMigrationResolutionView(
                **resolution_details(calculation, operation_id, revision).model_dump(),
                status="preview"
                if request.dry_run
                else "published"
                if checkpoint_id
                else "pending",
                result_checkpoint_id=checkpoint_id,
                result_directory_id=directory_id,
                migration_sequence=sequence,
                affected=affected,
            )
            if operation_id is not None:
                await self.repository.save_operation(
                    SkillMigrationResolutionOperation(
                        id=operation_id,
                        user_id=user_id,
                        account_id=migration.account_id,
                        migration_id=migration_id,
                        idempotency_key=request.idempotency_key,
                        request_digest=digest,
                        plan_revision=revision,
                        response_json=view.model_dump(mode="json"),
                    )
                )
            return view

    async def receipt_by_id(
        self, user_id: UUID, operation_id: UUID
    ) -> SkillMigrationResolutionReceipt:
        """
        按原受理身份授权查询，并复用不可变结果与当前诊断的组装。

        :param user_id (UUID): 当前用户
        :param operation_id (UUID): 原始完整解决受理身份
        :return SkillMigrationResolutionReceipt: 原始结果和独立当前状态
        """
        await self.queries.library.read_library(user_id)
        operation = await self.repository.operation_by_id(user_id, operation_id)
        if operation is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "migration resolution not found")
        return await self._receipt(operation, user_id)

    async def receipt(self, user_id: UUID, key: str) -> SkillMigrationResolutionReceipt:
        """
        读取原始完整解决响应，并单独报告迁移今天的状态。

        :param user_id (UUID): 当前用户
        :param key (str): 原始持久命令键
        :return SkillMigrationResolutionReceipt: 不可变解决回执和当前状态
        """
        await self.queries.library.read_library(user_id)
        operation = await self.repository.operation(user_id, key)
        if operation is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "migration resolution not found")
        return await self._receipt(operation, user_id)

    async def _receipt(
        self, operation: SkillMigrationResolutionOperation, user_id: UUID
    ) -> SkillMigrationResolutionReceipt:
        """
        保留完整解决原响应，迁移后续状态只进入独立字段。

        :param operation (SkillMigrationResolutionOperation): 已授权原操作
        :param user_id (UUID): 认证所有者
        :return SkillMigrationResolutionReceipt: 原始结果及当前诊断
        """
        result = _original(operation)
        migration = await self.planner.conflicts.require(user_id, operation.migration_id)
        return SkillMigrationResolutionReceipt.model_validate(
            {
                "result": result,
                "current_status": migration.status,
                "replacement_id": migration.replacement_id,
                "superseded_reason": migration.superseded_reason,
            }
        )


def _original(operation: SkillMigrationResolutionOperation) -> SkillMigrationResolutionView:
    """
    完整解决回执具有独立类型，不把历史 draft 响应误读成发布结果。

    :param operation (SkillMigrationResolutionOperation): 已授权用户操作记录
    :return SkillMigrationResolutionView: 原始完整解决响应
    """
    if operation.response_json.get("operation_kind") != "migration_resolution":
        raise SkillContentError(
            "OPERATION_KIND_MISMATCH", "operation is not a migration resolution"
        )
    return SkillMigrationResolutionView.model_validate(operation.response_json)
