"""
提交原操作的精确重试，独立查询既有重试回执。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.schemas.skill_deployment_retry import SkillDeploymentRetryRequest
from agent_remote_server.schemas.skill_results import SkillMutationData, SkillResult
from agent_remote_server.services.skills.deployment_retry import retry_deployment

router = APIRouter(prefix="/skills/operations", tags=["skills"])


@router.post("/{operation_id}/retries")
async def submit_retry(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    operation_id: UUID,
    payload: SkillDeploymentRetryRequest,
) -> SkillResult[SkillMutationData]:
    """
    原子追加全部选定目标的后继尝试，提交后返回原操作身份。

    :param context (SkillApiContext): 活跃用户事务
    :param operation_id (UUID): 原配置操作
    :param payload (SkillDeploymentRetryRequest): 已观察的精确尝试集合
    :return SkillResult[SkillMutationData]: 已提交受理的当前观察
    """
    await retry_deployment(context.session, context.user.id, operation_id, payload)
    result = await context.library().status(context.user.id, operation_id)
    await context.session.commit()
    return result


@router.get("/{operation_id}/retries")
async def retry_status(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    operation_id: UUID,
    key: Annotated[str, Query(min_length=1, max_length=128)],
) -> SkillResult[SkillMutationData]:
    """
    恢复既有重试回执，查询不追加尝试、不重新选择或调度目标。

    :param context (SkillApiContext): 活跃用户事务
    :param operation_id (UUID): 原配置操作
    :param key (str): 原请求的幂等键
    :return SkillResult[SkillMutationData]: 已验证回执对应的原操作
    """
    return await context.library().status_by_retry_key(context.user.id, operation_id, key)
