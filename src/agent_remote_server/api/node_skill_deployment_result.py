"""
以独立接口提交或只读观察原始部署结果，不复用通用任务完成。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_node, get_session, get_settings
from agent_remote_server.config import Settings
from agent_remote_server.models import Node
from agent_remote_server.schemas.skill_deployment_result import (
    SkillDeploymentPreparedResult,
    SkillDeploymentResultObservation,
)
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.services.skills.deployment_results import NodeDeploymentResults

router = APIRouter(prefix="/node/skill-deployments", tags=["node-skill-deployment"])


@router.post("/{attempt_id}/result")
async def confirm_deployment_result(
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    attempt_id: UUID,
    task_id: UUID,
    payload: SkillDeploymentPreparedResult,
) -> SkillResult[SkillDeploymentResultObservation]:
    """
    首次接受重验开关与租约；历史完全相同的回执可以只重放提交事实。

    :param node (Node): 已认证节点
    :param session (AsyncSession): 本次结果事务
    :param settings (Settings): 内容和能力配置
    :param attempt_id (UUID): 原尝试身份
    :param task_id (UUID): 精确任务身份
    :param payload (SkillDeploymentPreparedResult): 原始准备结果
    :return SkillResult[SkillDeploymentResultObservation]: 已确认的原回执
    """
    result = await NodeDeploymentResults(session, settings).confirm(
        node.id, task_id, attempt_id, payload
    )
    await session.commit()
    return SkillResult(status="confirmed", committed=True, data=result)


@router.post("/{attempt_id}/result/inspect")
async def inspect_deployment_result(
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    attempt_id: UUID,
    task_id: UUID,
    payload: SkillDeploymentPreparedResult,
) -> SkillResult[SkillDeploymentResultObservation]:
    """
    历史元数据观察不依赖新部署开关，也不修改租约、引用或目标阶段。

    :param node (Node): 已认证原节点
    :param session (AsyncSession): 当前只读观察事务
    :param settings (Settings): 服务配置
    :param attempt_id (UUID): 原尝试身份
    :param task_id (UUID): 精确任务身份
    :param payload (SkillDeploymentPreparedResult): 待确认的精确结果
    :return SkillResult[SkillDeploymentResultObservation]: 不授予执行权的提交观察
    """
    result = await NodeDeploymentResults(session, settings).inspect(
        node.id, task_id, attempt_id, payload
    )
    await session.commit()
    return SkillResult(status="observed", committed=False, data=result)
