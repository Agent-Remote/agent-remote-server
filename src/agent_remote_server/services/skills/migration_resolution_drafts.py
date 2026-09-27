"""
内部迁移计划编辑与不可变回执共享保存点，完整候选不冒充原子发布结果。
"""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_migration_resolution import SkillMigrationResolutionOperation
from agent_remote_server.schemas.skill_migration_resolution import (
    SkillMigrationResolutionDraftReceipt,
    SkillMigrationResolutionDraftView,
)
from agent_remote_server.schemas.skill_resolution import SkillResolutionRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_resolution_choices import choice_row, choice_spec
from agent_remote_server.services.skills.migration_resolution_plan import MigrationResolutionPlanner
from agent_remote_server.services.skills.migration_resolution_views import resolution_details
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class MigrationResolutionDraftService:
    """
    仅保存计划；最终解决器须在更外层事务中接入身份授权和完整发布。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        共用候选计算器、用户锁和迁移专用计划仓储。

        :param session (AsyncSession): 外层请求事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 内容额度和目录限制
        """
        self._session = session
        self.planner = MigrationResolutionPlanner(session, store, policy)
        self.repository = self.planner.repository
        self.queries = self.planner.conflicts.queries

    async def execute(
        self, user_id: UUID, migration_id: UUID, request: SkillResolutionRequest
    ) -> SkillMigrationResolutionDraftView:
        """
        替换重叠选择并原子保存版本和原响应，重放不重新解释今天的状态。

        :param user_id (UUID): 已认证用户
        :param migration_id (UUID): 原迁移冲突身份
        :param request (SkillResolutionRequest): 单次明确编辑及持久键
        :return SkillMigrationResolutionDraftView: 内部预览或保存结果，不表示发布
        """
        digest = hashlib.sha256(
            json.dumps(
                {
                    "kind": "migration_resolution_draft",
                    "migration_id": str(migration_id),
                    "request": request.model_dump(mode="json"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self._session.begin_nested():
            if request.dry_run:
                await self.queries.library.read_library(user_id)
            else:
                await self.queries.library.lock_library(user_id)
                previous = await self.repository.operation(user_id, request.idempotency_key)
                if previous is not None:
                    if previous.request_digest != digest:
                        raise SkillContentError(
                            "IDEMPOTENCY_CONFLICT", "key belongs to another migration resolution"
                        )
                    return _original(previous)
            migration = await self.planner.conflicts.require(user_id, migration_id)
            plan = await self.repository.plan(migration)
            revision = plan.revision if plan else 0
            if revision != request.expected_revision:
                raise SkillContentError(
                    "PLAN_REVISION_CONFLICT",
                    "migration resolution plan changed",
                    details={"expected": request.expected_revision, "current": revision},
                )
            calculation = await self.planner.calculate(
                user_id,
                migration_id,
                [choice_spec(row) for row in await self.repository.choices(migration)],
                replacement=request.choice,
            )
            if not request.dry_run:
                if not await self.repository.replace_choices(
                    migration,
                    revision,
                    [choice_row(migration, choice) for choice in calculation.choices],
                ):
                    raise SkillContentError("PLAN_REVISION_CONFLICT", "plan changed during edit")
                revision += 1
            view = SkillMigrationResolutionDraftView(
                **resolution_details(
                    calculation, None if request.dry_run else uuid4(), revision
                ).model_dump(),
                status="preview" if request.dry_run else "planned",
            )
            if view.operation_id is not None:
                await self.repository.save_operation(
                    SkillMigrationResolutionOperation(
                        id=view.operation_id,
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

    async def receipt(self, user_id: UUID, key: str) -> SkillMigrationResolutionDraftReceipt:
        """
        只读恢复原始计划结果，另列当前迁移状态而不把后续选择写进旧回执。

        :param user_id (UUID): 当前用户
        :param key (str): 用户保存的原始命令键
        :return SkillMigrationResolutionDraftReceipt: 不可变编辑响应及当前迁移状态
        """
        await self.queries.library.read_library(user_id)
        operation = await self.repository.operation(user_id, key)
        if operation is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "migration resolution not found")
        result = _original(operation)
        migration = await self.planner.conflicts.require(user_id, operation.migration_id)
        return SkillMigrationResolutionDraftReceipt.model_validate(
            {"result": result, "current_status": migration.status}
        )


def _original(operation: SkillMigrationResolutionOperation) -> SkillMigrationResolutionDraftView:
    """
    类型标签是回执协议的一部分，不能把未来发布结果或其他操作解释成草稿。

    :param operation (SkillMigrationResolutionOperation): 已授权用户键对应的不可变记录
    :return SkillMigrationResolutionDraftView: 原始内部编辑响应
    """
    if operation.response_json.get("operation_kind") != "migration_resolution_draft":
        raise SkillContentError("OPERATION_KIND_MISMATCH", "operation is not a migration draft")
    return SkillMigrationResolutionDraftView.model_validate(operation.response_json)
