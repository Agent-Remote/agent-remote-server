"""
向原认证 Node 提供独立部署撤权、排空确认及只读恢复。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_node, get_session
from agent_remote_server.models import Node
from agent_remote_server.schemas.skill_deployment_termination import (
    SkillDeploymentTerminatedResult,
    SkillDeploymentTerminationIntent,
    SkillDeploymentTerminationLookup,
    SkillDeploymentTerminationObservation,
    SkillDeploymentTerminationRequest,
)
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.services.skills.deployment_termination import NodeDeploymentTermination

router = APIRouter(prefix="/node/skill-deployments", tags=["node-skill-deployment"])


@router.post("/{attempt_id}/termination")
async def request_deployment_termination(
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    attempt_id: UUID,
    task_id: UUID,
    payload: SkillDeploymentTerminationRequest,
) -> SkillResult[SkillDeploymentTerminationIntent]:
    """
    原领取持久撤权后才返回排空指令，不将任务标为已结束。

    :param node (Node): 已认证原节点
    :param session (AsyncSession): 原请求事务
    :param attempt_id (UUID): 原尝试身份
    :param task_id (UUID): 原任务数据库身份
    :param payload (SkillDeploymentTerminationRequest): 原始撤权请求
    :return SkillResult[SkillDeploymentTerminationIntent]: 已持久保存的撤权指令
    """
    intent = await NodeDeploymentTermination(session).request(node.id, task_id, attempt_id, payload)
    await session.commit()
    return SkillResult(status="drain_required", committed=True, data=intent)


@router.get("/{attempt_id}/termination")
async def inspect_deployment_termination(
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    attempt_id: UUID,
    task_id: UUID,
) -> SkillResult[SkillDeploymentTerminationLookup]:
    """
    只读恢复原意图，不续租或授予新的准备权限。

    :param node (Node): 已认证原节点
    :param session (AsyncSession): 原观察事务
    :param attempt_id (UUID): 原尝试身份
    :param task_id (UUID): 原任务数据库身份
    :return SkillResult[SkillDeploymentTerminationLookup]: 原始撤权指令或尚未撤权
    """
    intent = await NodeDeploymentTermination(session).lookup(node.id, task_id, attempt_id)
    await session.commit()
    return SkillResult(
        status="observed", committed=False, data=SkillDeploymentTerminationLookup(intent=intent)
    )


@router.post("/{attempt_id}/termination/result")
async def confirm_deployment_termination(
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    attempt_id: UUID,
    task_id: UUID,
    payload: SkillDeploymentTerminatedResult,
) -> SkillResult[SkillDeploymentTerminationObservation]:
    """
    精确原指令和本地永久排空凭据共同提交独立终态。

    :param node (Node): 已认证原节点
    :param session (AsyncSession): 当前终态事务
    :param attempt_id (UUID): 原尝试身份
    :param task_id (UUID): 原任务数据库身份
    :param payload (SkillDeploymentTerminatedResult): 原始排空结果
    :return SkillResult[SkillDeploymentTerminationObservation]: 已确认的原始终态
    """
    result = await NodeDeploymentTermination(session).confirm(node.id, task_id, attempt_id, payload)
    await session.commit()
    return SkillResult(status="confirmed", committed=True, data=result)


@router.post("/{attempt_id}/termination/result/inspect")
async def inspect_deployment_termination_result(
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    attempt_id: UUID,
    task_id: UUID,
    payload: SkillDeploymentTerminatedResult,
) -> SkillResult[SkillDeploymentTerminationObservation]:
    """
    只读确认同一个原排空结果是否已持久接受。

    :param node (Node): 已认证原节点
    :param session (AsyncSession): 当前观察事务
    :param attempt_id (UUID): 原尝试身份
    :param task_id (UUID): 原任务数据库身份
    :param payload (SkillDeploymentTerminatedResult): 待确认的原终态
    :return SkillResult[SkillDeploymentTerminationObservation]: 不授予执行权的历史观察
    """
    result = await NodeDeploymentTermination(session).inspect(node.id, task_id, attempt_id, payload)
    await session.commit()
    return SkillResult(status="observed", committed=False, data=result)
