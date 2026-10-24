"""
在同步动作事务内绑定用户已确认的目标。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.errors import ApiError
from agent_remote_server.models import Node, SyncSession
from agent_remote_server.schemas.workspaces import SyncSessionExpectedTarget


def sync_remote_endpoint(node: Node | None, sync: SyncSession) -> str | None:
    """
    使用相同规则构造查询响应与动作确认的端点。

    :param node (Node | None): 当前节点
    :param sync (SyncSession): 当前同步会话
    :return str | None: 远端端点
    """
    if node is None or not (node.ssh_host or node.wireguard_ip):
        return None
    user = node.ssh_user or "agent-remote"
    host = node.ssh_host or node.wireguard_ip
    return f"{user}@{host}:{node.ssh_port or 22}:{sync.remote_path}"


async def lock_sync_target(
    session: AsyncSession,
    user_id: UUID,
    sync_id: UUID,
    expected: SyncSessionExpectedTarget | None,
) -> tuple[SyncSession, Node | None]:
    """
    锁定当前会话和节点后比较确认目标。

    :param session (AsyncSession): 当前动作事务
    :param user_id (UUID): 已认证用户
    :param sync_id (UUID): 请求会话
    :param expected (SyncSessionExpectedTarget | None): 可选确认目标
    :return tuple[SyncSession, Node | None]: 已锁定会话及节点
    """
    sync = await session.scalar(
        select(SyncSession)
        .where(SyncSession.id == sync_id, SyncSession.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if sync is None:
        raise ApiError(
            code="COMMON_NOT_FOUND", message="Sync session was not found.", status_code=404
        )
    node = None
    if sync.node_id is not None:
        node = await session.scalar(
            select(Node)
            .where(Node.id == sync.node_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    current = SyncSessionExpectedTarget(
        id=sync.id,
        workspace_id=sync.workspace_id,
        node_id=sync.node_id,
        local_path=sync.local_path,
        remote_path=sync.remote_path,
        mutagen_session_id=sync.mutagen_session_id,
        remote_endpoint=sync_remote_endpoint(node, sync),
        sync_mode=sync.sync_mode,
        sync_git=sync.sync_git,
        exclude=sync.exclude_patterns,
    )
    if expected is not None and current != expected:
        raise ApiError(
            code="SYNC_TARGET_CHANGED",
            message="Sync target changed; review it again.",
            status_code=409,
        )
    return sync, node
