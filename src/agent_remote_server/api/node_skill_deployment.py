"""
提供独立部署的完整输入、精确文件与短期续租，不接收任意宿主路径。
"""

import tempfile
from dataclasses import dataclass
from functools import partial
from typing import Annotated, BinaryIO, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from agent_remote_server.api.deps import get_current_node, get_session, get_settings
from agent_remote_server.api.skill_streams import download_chunks
from agent_remote_server.config import Settings
from agent_remote_server.models import Node
from agent_remote_server.schemas.skill_deployment_content import (
    SkillDeploymentContent,
    SkillDeploymentLease,
    SkillDeploymentLeaseRequest,
)
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_content import NodeDeploymentContent
from agent_remote_server.skill_manager.storage.io import run_storage_io

router = APIRouter(prefix="/node/skill-deployments", tags=["node-skill-deployment"])
MAX_DEPLOYMENT_ENVELOPE_BYTES = 64 * 1024 * 1024


@dataclass
class NodeDeploymentContext:
    """
    所有身份来自节点认证依赖，调用者不能选择其他用户的输入。
    """

    node: Node
    session: AsyncSession
    service: NodeDeploymentContent


def deployment_context(
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> NodeDeploymentContext:
    """
    认证与开关检查必须先于任何私有内容访问。

    :param node (Node): 已认证节点
    :param session (AsyncSession): 请求事务
    :param settings (Settings): 服务端部署配置
    :return NodeDeploymentContext: 当前请求的独立部署上下文
    """
    if not settings.skill_manager_enabled:
        raise SkillContentError("SKILL_MANAGER_DISABLED", "skill management API is not enabled")
    return NodeDeploymentContext(node, session, NodeDeploymentContent(session, settings))


@router.get("/{attempt_id}", response_model=SkillResult[SkillDeploymentContent])
async def deployment_manifest(
    context: Annotated[NodeDeploymentContext, Depends(deployment_context)],
    attempt_id: UUID,
    task_id: UUID,
    lease_attempt: Annotated[int, Query(ge=1, le=2147483647)],
) -> Response:
    """
    对编码后封套执行硬上限，不能通过超大清单占用无界传输内存。

    :param context (NodeDeploymentContext): 认证上下文
    :param attempt_id (UUID): 原部署尝试
    :param task_id (UUID): 精确任务数据库身份
    :param lease_attempt (int): 当前领取轮次
    :return Response: 不声明执行结果的完整固定输入
    """
    data = await context.service.describe(context.node.id, task_id, attempt_id, lease_attempt)
    body = (
        SkillResult(status="prepared_input", committed=False, data=data).model_dump_json().encode()
    )
    if len(body) > MAX_DEPLOYMENT_ENVELOPE_BYTES:
        raise SkillContentError("CONTENT_TOO_LARGE", "deployment input exceeds transport limit")
    await context.session.commit()
    return Response(body, media_type="application/json")


@router.get("/{attempt_id}/files/{digest}")
async def deployment_file(
    context: Annotated[NodeDeploymentContext, Depends(deployment_context)],
    attempt_id: UUID,
    digest: str,
    task_id: UUID,
    lease_attempt: Annotated[int, Query(ge=1, le=2147483647)],
) -> StreamingResponse:
    """
    完整验证后重验同一领取权限，磁盘等待不持有用户或任务锁。

    :param context (NodeDeploymentContext): 认证上下文
    :param attempt_id (UUID): 原部署尝试
    :param digest (str): 原清单内文件摘要
    :param task_id (UUID): 精确任务数据库身份
    :param lease_attempt (int): 当前领取轮次
    :return StreamingResponse: 完整验证的二进制文件
    """
    target = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    identity = (context.node.id, task_id, attempt_id, lease_attempt)
    try:
        download = await context.service.authorize_file(*identity, digest)
        await context.session.commit()
        await context.service.copy_authorized_file(download, cast(BinaryIO, target))
        await context.service.authorize(*identity)
        size = await run_storage_io(target.tell)
        await run_storage_io(partial(target.seek, 0))
        await context.session.commit()
    except BaseException:
        await run_storage_io(target.close)
        raise
    return StreamingResponse(
        download_chunks(cast(BinaryIO, target)),
        media_type="application/octet-stream",
        headers={"Content-Length": str(size), "ETag": f'"{digest}"'},
        background=BackgroundTask(target.close),
    )


@router.post("/{attempt_id}/lease")
async def deployment_lease(
    context: Annotated[NodeDeploymentContext, Depends(deployment_context)],
    attempt_id: UUID,
    task_id: UUID,
    payload: SkillDeploymentLeaseRequest,
) -> SkillResult[SkillDeploymentLease]:
    """
    明确提交当前领取的短期续租，不改变尝试、内容或实际使用状态。

    :param context (NodeDeploymentContext): 认证上下文
    :param attempt_id (UUID): 原部署尝试
    :param task_id (UUID): 精确任务数据库身份
    :param payload (SkillDeploymentLeaseRequest): 当前领取轮次
    :return SkillResult[SkillDeploymentLease]: 同一原始输入的短期凭据
    """
    data = await context.service.renew(context.node.id, task_id, attempt_id, payload.lease_attempt)
    await context.session.commit()
    return SkillResult(status="leased", committed=False, data=data)
