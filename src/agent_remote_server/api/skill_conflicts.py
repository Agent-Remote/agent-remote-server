"""
提供用户授权的冲突查询、元数据差异和原子计划解决入口。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.schemas.skill_conflicts import (
    SkillConflictDiff,
    SkillConflictPage,
    SkillConflictView,
    SkillResolutionView,
)
from agent_remote_server.schemas.skill_resolution import SkillResolutionRequest
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.services.skills.conflicts import SkillConflictService
from agent_remote_server.services.skills.resolution import SkillResolutionService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(prefix="/skills/state", tags=["skill-state"])


def conflict_service(context: SkillApiContext) -> SkillConflictService:
    """
    构造与当前用户请求共用事务的查询服务。

    :param context (SkillApiContext): 已认证用户上下文
    :return SkillConflictService: 同事务查询服务
    """
    return SkillConflictService(
        context.session,
        PrivateObjectStore(context.settings.skill_storage_root),
        context.settings.skill_storage_policy,
    )


@router.get("/conflicts")
async def list_conflicts(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    account_id: UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: UUID | None = None,
    skill: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> SkillResult[SkillConflictPage]:
    """
    显式按账户分页读取本用户未解决及已取代的冲突。

    :param context (SkillApiContext): 用户授权上下文
    :param account_id (UUID): 唯一账户范围
    :param limit (int): 有界页大小
    :param cursor (UUID | None): 同账户翻页游标
    :param skill (str | None): 可选技能名称或稳定来源身份
    :return SkillResult[SkillConflictPage]: 元数据页
    """
    result = await conflict_service(context).list(context.user.id, account_id, limit, cursor, skill)
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.get("/conflicts/{publication_id}")
async def conflict_info(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    publication_id: UUID,
) -> SkillResult[SkillConflictView]:
    """
    解释不可变三侧来源、目标分支以及保存的解决计划。

    :param context (SkillApiContext): 用户授权上下文
    :param publication_id (UUID): 原始冲突尝试
    :return SkillResult[SkillConflictView]: 完整授权元数据
    """
    result = await conflict_service(context).info(context.user.id, publication_id)
    await context.session.commit()
    return SkillResult(status=result.status, committed=False, data=result)


@router.get("/conflicts/{publication_id}/diff")
async def conflict_diff(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    publication_id: UUID,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
) -> SkillResult[SkillConflictDiff]:
    """
    二进制及大文件只返回摘要和大小，不插入或生成合并正文。

    :param context (SkillApiContext): 用户授权上下文
    :param publication_id (UUID): 原始尝试
    :param limit (int): 路径页大小
    :param cursor (str | None): 相对路径游标
    :return SkillResult[SkillConflictDiff]: 明确三侧的元数据差异
    """
    result = await conflict_service(context).diff(context.user.id, publication_id, limit, cursor)
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.post("/conflicts/{publication_id}/resolve")
async def resolve_conflict(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    publication_id: UUID,
    payload: SkillResolutionRequest,
) -> SkillResult[SkillResolutionView]:
    """
    每次选择先保存计划，完整通过验证后才与所有目标 head 一起提交。

    :param context (SkillApiContext): 用户授权上下文
    :param publication_id (UUID): 原始冲突尝试
    :param payload (SkillResolutionRequest): 明确解决或预览请求
    :return SkillResult[SkillResolutionView]: 稳定计划、预览或发布结果
    """
    service = SkillResolutionService(
        context.session,
        PrivateObjectStore(context.settings.skill_storage_root),
        context.settings.skill_storage_policy,
    )
    result = await service.execute(context.user.id, publication_id, payload)
    await context.session.commit()
    return SkillResult(
        status=result.status,
        committed=not payload.dry_run,
        operation_id=result.operation_id,
        data=result,
    )


@router.get("/resolution-operations")
async def resolution_operation(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    key: str,
) -> SkillResult[SkillResolutionView]:
    """
    查询原幂等回执，后续计划变更不改写先前接受结果。

    :param context (SkillApiContext): 用户授权上下文
    :param key (str): 客户端持久化键
    :return SkillResult[SkillResolutionView]: 原始已提交结果
    """
    result = await conflict_service(context).operation(context.user.id, key)
    return SkillResult(
        status=result.status, committed=True, operation_id=result.operation_id, data=result
    )


@router.get("/resolution-operations/{operation_id}")
async def resolution_operation_by_id(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    operation_id: UUID,
) -> SkillResult[SkillResolutionView]:
    """
    按原受理身份查询不可变结果，不保存选择或重新计算。

    :param context (SkillApiContext): 已认证用户上下文
    :param operation_id (UUID): 原始解决受理身份
    :return SkillResult[SkillResolutionView]: 原受理回执
    """
    result = await conflict_service(context).operation_by_id(context.user.id, operation_id)
    await context.session.commit()
    return SkillResult(
        status=result.status, committed=True, operation_id=result.operation_id, data=result
    )
