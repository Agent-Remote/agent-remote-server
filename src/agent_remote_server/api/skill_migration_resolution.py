"""
提供用户级迁移原子解决与独立类型回执，历史受理和当前迁移状态分别说明。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.schemas.skill_migration_resolution import (
    SkillMigrationResolutionReceipt,
    SkillMigrationResolutionView,
)
from agent_remote_server.schemas.skill_resolution import SkillResolutionRequest
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.services.skills.migration_resolution import SkillMigrationResolutionService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(prefix="/skills/state/migration", tags=["skill-state"])


def migration_resolution(context: SkillApiContext) -> SkillMigrationResolutionService:
    """
    构造与认证请求共享保存点、内容卷和策略的完整解决服务。

    :param context (SkillApiContext): 已认证用户及请求事务
    :return SkillMigrationResolutionService: 用户范围原子服务
    """
    return SkillMigrationResolutionService(
        context.session,
        PrivateObjectStore(context.settings.skill_storage_root),
        context.settings.skill_storage_policy,
    )


@router.post("/conflicts/{migration_id}/resolve")
async def resolve_migration_conflict(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    migration_id: UUID,
    payload: SkillResolutionRequest,
) -> SkillResult[SkillMigrationResolutionView]:
    """
    完整候选才推进所有关联 head，不完整选择仅保存计划及不可变回执。

    :param context (SkillApiContext): 用户上下文
    :param migration_id (UUID): 独立迁移冲突身份
    :param payload (SkillResolutionRequest): 明确选择、计划版本和持久键
    :return SkillResult[SkillMigrationResolutionView]: 预览、保存计划或完整发布结果
    """
    result = await migration_resolution(context).execute(context.user.id, migration_id, payload)
    await context.session.commit()
    return SkillResult(
        status=result.status,
        committed=not payload.dry_run,
        operation_id=result.operation_id,
        data=result,
    )


@router.get("/resolution-operations")
async def migration_resolution_operation(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    key: Annotated[str, Query(min_length=1, max_length=128, pattern=r"^[!-~]+$")],
) -> SkillResult[SkillMigrationResolutionReceipt]:
    """
    返回原始接受结果，另行标注当前状态，不重新执行历史选择。

    :param context (SkillApiContext): 用户上下文
    :param key (str): 原始持久化命令键
    :return SkillResult[SkillMigrationResolutionReceipt]: 原回执与当前迁移状态
    """
    receipt = await migration_resolution(context).receipt(context.user.id, key)
    await context.session.commit()
    return SkillResult(
        status=receipt.result.status,
        committed=True,
        operation_id=receipt.result.operation_id,
        data=receipt,
    )


@router.get("/resolution-operations/{operation_id}")
async def migration_resolution_operation_by_id(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    operation_id: UUID,
) -> SkillResult[SkillMigrationResolutionReceipt]:
    """
    按原受理身份读取完整解决回执和迁移当前状态。

    :param context (SkillApiContext): 已认证用户上下文
    :param operation_id (UUID): 原始完整解决受理身份
    :return SkillResult[SkillMigrationResolutionReceipt]: 不可变原响应及独立诊断
    """
    receipt = await migration_resolution(context).receipt_by_id(context.user.id, operation_id)
    await context.session.commit()
    return SkillResult(
        status=receipt.result.status,
        committed=True,
        operation_id=receipt.result.operation_id,
        data=receipt,
    )
