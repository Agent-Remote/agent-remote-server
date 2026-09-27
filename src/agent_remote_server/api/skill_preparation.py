"""
提供同账户有效版本准备和独立迁移结果查询。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.schemas.skill_preparation import (
    SkillPreparationReceipt,
    SkillPreparationRequest,
    SkillPreparationView,
)
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.services.skills.preparation import SkillPreparationService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(prefix="/skills/state", tags=["skill-state"])


def preparation_service(context: SkillApiContext) -> SkillPreparationService:
    """
    复用现有用户认证、功能开关与私有内容策略。

    :param context (SkillApiContext): 已认证上下文
    :return SkillPreparationService: 同事务准备服务
    """
    return SkillPreparationService(
        context.session,
        PrivateObjectStore(context.settings.skill_storage_root),
        context.settings.skill_storage_policy,
    )


@router.post("/prepare")
async def prepare_branch(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    payload: SkillPreparationRequest,
) -> SkillResult[SkillPreparationView]:
    """
    冲突也先完整提交受理记录，不借异常回滚丢弃迁移输入。

    :param context (SkillApiContext): 已认证用户上下文
    :param payload (SkillPreparationRequest): 精确目标、期望条件及幂等键
    :return SkillResult[SkillPreparationView]: 无写入预览或完整受理结果
    """
    result = await preparation_service(context).execute(context.user.id, payload)
    await context.session.commit()
    return SkillResult(
        status=result.status,
        committed=not payload.dry_run,
        operation_id=result.operation_id,
        data=result,
    )


@router.get("/preparations")
async def preparation_receipt(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    key: Annotated[str, Query(min_length=1, max_length=128)],
) -> SkillResult[SkillPreparationReceipt]:
    """
    原始回执查询不重新选择版本或运行计划。

    :param context (SkillApiContext): 用户上下文
    :param key (str): 原始受理幂等键
    :return SkillResult[SkillPreparationReceipt]: 已授权原始结果与当前状态
    """
    result = await preparation_service(context).receipt(context.user.id, key)
    return SkillResult(
        status=result.current_status,
        committed=True,
        operation_id=result.result.operation_id,
        data=result,
    )
