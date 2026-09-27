"""
提供节点精确快照清单和文件流，不接受任意用户内容寻址。
"""

import tempfile
from dataclasses import dataclass
from functools import partial
from typing import Annotated, BinaryIO, cast
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from agent_remote_server.api.deps import get_current_node, get_session, get_settings
from agent_remote_server.api.skill_streams import download_chunks
from agent_remote_server.config import Settings
from agent_remote_server.models import Node
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.schemas.skill_snapshot_lease import (
    SkillSnapshotLease,
    SkillSnapshotLeaseRequest,
)
from agent_remote_server.schemas.skill_snapshots import SkillSnapshotView
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.node_content import NodeSkillContentService
from agent_remote_server.skill_manager.storage.io import run_storage_io
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(prefix="/node/skill-snapshots", tags=["node-skill-content"])


@dataclass
class NodeSkillContext:
    """
    身份由节点认证依赖固定，服务仅允许读取该节点的准备树。
    """

    node: Node
    session: AsyncSession
    service: NodeSkillContentService
    lease_seconds: int


def get_node_skill_context(
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> NodeSkillContext:
    """
    在清单或文件访问之前完成节点认证和部署开关检查。

    :param node (Node): 认证节点
    :param session (AsyncSession): 请求事务
    :param settings (Settings): 部署配置
    :return NodeSkillContext: 精确节点内容上下文
    """
    if not settings.skill_manager_enabled:
        raise SkillContentError("SKILL_MANAGER_DISABLED", "skill management API is not enabled")
    return NodeSkillContext(
        node,
        session,
        NodeSkillContentService(
            session, PrivateObjectStore(settings.skill_storage_root), settings.skill_storage_policy
        ),
        settings.node_task_lease_seconds,
    )


@router.get("/{snapshot_id}")
async def snapshot_manifest(
    context: Annotated[NodeSkillContext, Depends(get_node_skill_context)],
    snapshot_id: UUID,
    task_id: UUID,
) -> SkillResult[SkillSnapshotView]:
    """
    返回仅属于当前有效准备任务的完整快照。

    :param context (NodeSkillContext): 认证节点上下文
    :param snapshot_id (UUID): 请求快照
    :param task_id (UUID): 精确准备任务
    :return SkillResult[SkillSnapshotView]: 不含宿主路径的准备封套
    """
    data = await context.service.describe(context.node.id, snapshot_id, task_id)
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=data)


@router.get("/{snapshot_id}/files/{digest}")
async def snapshot_file(
    context: Annotated[NodeSkillContext, Depends(get_node_skill_context)],
    snapshot_id: UUID,
    digest: str,
    task_id: UUID,
) -> StreamingResponse:
    """
    先按固定树验证完整文件，再从私有磁盘暂存发送有界流。

    :param context (NodeSkillContext): 认证节点上下文
    :param snapshot_id (UUID): 请求快照
    :param digest (str): 该快照清单内的文件摘要
    :param task_id (UUID): 精确准备任务
    :return StreamingResponse: 带摘要和长度的二进制响应
    """
    target = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        node_id = context.node.id
        download = await context.service.authorize_file(node_id, snapshot_id, task_id, digest)
        await context.session.commit()
        await context.service.copy_authorized_file(download, cast(BinaryIO, target))
        await context.service.authorize(node_id, snapshot_id, task_id)
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


@router.post("/{snapshot_id}/lease")
async def renew_snapshot_lease(
    context: Annotated[NodeSkillContext, Depends(get_node_skill_context)],
    snapshot_id: UUID,
    task_id: UUID,
    payload: SkillSnapshotLeaseRequest,
) -> SkillResult[SkillSnapshotLease]:
    """
    独立事务只续期当前领取轮次，不授予内容发布或运行重放权限。

    :param context (NodeSkillContext): 认证节点上下文
    :param snapshot_id (UUID): 原始固定快照
    :param task_id (UUID): 精确数据库任务身份
    :param payload (SkillSnapshotLeaseRequest): 当前领取序号
    :return SkillResult[SkillSnapshotLease]: 未改变快照的短期授权
    """
    data = await context.service.renew_lease(
        context.node.id, snapshot_id, task_id, payload, context.lease_seconds
    )
    await context.session.commit()
    return SkillResult(status="leased", committed=False, data=data)
