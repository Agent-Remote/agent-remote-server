"""
提供同账户任意明确版本之间的增量迁移和原始回执查询。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.schemas.skill_migration import (
    SkillMigrationPrecondition,
    SkillMigrationReceipt,
    SkillMigrationRequest,
    SkillMigrationSelector,
    SkillMigrationView,
)
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.services.skills.migration import SkillMigrationService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(prefix="/skills/state", tags=["skill-state"])


def migration_service(context: SkillApiContext) -> SkillMigrationService:
    """
    使用既有用户认证和功能开关构造同事务迁移服务。

    :param context (SkillApiContext): 已认证上下文
    :return SkillMigrationService: 显式迁移服务
    """
    return SkillMigrationService(
        context.session,
        PrivateObjectStore(context.settings.skill_storage_root),
        context.settings.skill_storage_policy,
    )


@router.get("/migration/current")
async def migration_current(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    account_id: UUID,
    skill: Annotated[str, Query(min_length=1, max_length=64)],
    from_revision: Annotated[str, Query(min_length=1, max_length=64)],
    to_revision: Annotated[str, Query(min_length=1, max_length=64)],
) -> SkillResult[SkillMigrationPrecondition]:
    """
    查询完整双方状态和上次成功来源基线，不改变 pin 或启用状态。

    :param context (SkillApiContext): 用户上下文
    :param account_id (UUID): 所属账户
    :param skill (str): 稳定来源选择
    :param from_revision (str): 来源版本
    :param to_revision (str): 目标版本
    :return SkillResult[SkillMigrationPrecondition]: 无写入的完整前置条件
    """
    result = await migration_service(context).selection.current(
        context.user.id,
        SkillMigrationSelector(
            account_id=account_id, skill=skill, from_revision=from_revision, to_revision=to_revision
        ),
    )
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.post("/migrate")
async def migrate_state(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], payload: SkillMigrationRequest
) -> SkillResult[SkillMigrationView]:
    """
    仅完整成功才更新迁移基线，冲突则提交可恢复原始输入。

    :param context (SkillApiContext): 用户上下文
    :param payload (SkillMigrationRequest): 明确版本及完整前置条件
    :return SkillResult[SkillMigrationView]: 预览、完整成功或持久化冲突
    """
    result = await migration_service(context).execute(context.user.id, payload)
    await context.session.commit()
    return SkillResult(
        status=result.status,
        committed=not payload.dry_run,
        operation_id=result.operation_id,
        data=result,
    )


@router.get("/migration/operations")
async def migration_receipt(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    key: Annotated[str, Query(min_length=1, max_length=128)],
) -> SkillResult[SkillMigrationReceipt]:
    """
    根据所有者和原始键恢复幂等结果，不运行旧迁移。

    :param context (SkillApiContext): 已认证用户
    :param key (str): 持久化命令键
    :return SkillResult[SkillMigrationReceipt]: 原始结果及当前失效状态
    """
    result = await migration_service(context).receipt(context.user.id, key)
    return SkillResult(
        status=result.current_status,
        committed=True,
        operation_id=result.result.operation_id,
        data=result,
    )


@router.get("/migration/operations/{operation_id}")
async def migration_receipt_by_id(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], operation_id: UUID
) -> SkillResult[SkillMigrationReceipt]:
    """
    按原始身份恢复迁移回执，授权和键查询保持一致。

    :param context (SkillApiContext): 已认证用户
    :param operation_id (UUID): 原始增量操作身份
    :return SkillResult[SkillMigrationReceipt]: 不可变结果及当前失效状态
    """
    result = await migration_service(context).receipt_by_id(context.user.id, operation_id)
    return SkillResult(
        status=result.current_status,
        committed=True,
        operation_id=result.result.operation_id,
        data=result,
    )
