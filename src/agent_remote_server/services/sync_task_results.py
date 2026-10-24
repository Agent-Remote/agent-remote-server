"""
按准备任务的完整目标条件发布同步状态，防止迟到结果覆盖暂停。
"""

from typing import Literal
from uuid import UUID

from sqlalchemy import exists, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask, SyncSession, Workspace


async def publish_sync_preparation(
    session: AsyncSession, task: NodeTask, status: Literal["active", "failed"]
) -> None:
    """
    仅更新仍在等待原准备任务的同步会话。

    :param session (AsyncSession): 当前结果事务
    :param task (NodeTask): 已认证节点的原任务
    :param status (Literal["active", "failed"]): 准备结果状态
    """
    if task.task_type != "prepare_workspace":
        return
    values = [
        task.payload.get(name)
        for name in ("sync_session_id", "workspace_id", "user_id", "device_id")
    ]
    if not all(isinstance(value, str) for value in values):
        return
    try:
        sync_id, workspace_id, user_id, device_id = [UUID(str(value)) for value in values]
    except ValueError:
        return
    remote_path = task.payload.get("remote_path")
    if not isinstance(remote_path, str) or not remote_path:
        return
    if task.task_id != f"prepare_workspace:{sync_id}":
        return
    # SQL 条件在写入时重查生命周期，避免预读对象掩盖并发暂停。
    await session.execute(
        update(SyncSession)
        .where(
            SyncSession.id == sync_id,
            SyncSession.status == "starting",
            SyncSession.workspace_id == workspace_id,
            SyncSession.user_id == user_id,
            SyncSession.node_id == task.node_id,
            SyncSession.remote_path == remote_path,
            exists().where(
                Workspace.id == workspace_id,
                Workspace.user_id == user_id,
                Workspace.device_id == device_id,
                Workspace.remote_path == remote_path,
            ),
        )
        .values(status=status)
        .execution_options(synchronize_session=False)
    )
