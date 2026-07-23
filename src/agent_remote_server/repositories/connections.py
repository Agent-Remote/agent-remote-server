from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import (
    Node,
    Session,
    SshKey,
    SyncSession,
    ToolAccount,
    ToolAccountProfile,
    UserDevice,
    WireGuardPeer,
    Workspace,
)


class ConnectionRepository:
    """
    WireGuard 和 SSH 连接仓储
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_device(self, *, user_id: UUID, device_id: UUID) -> UserDevice | None:
        """
        读取当前用户的活跃设备

        :param user_id (UUID): 用户 ID
        :param device_id (UUID): 设备 ID

        :return UserDevice: 设备实体
        """

        return await self._session.scalar(
            select(UserDevice)
            .where(UserDevice.id == device_id)
            .where(UserDevice.user_id == user_id)
            .where(UserDevice.status == "active")
        )

    async def get_active_device_by_id(self, device_id: UUID) -> UserDevice | None:
        """
        按 ID 读取活跃设备

        :param device_id (UUID): 设备 ID

        :return UserDevice: 活跃设备实体
        """

        return await self._session.scalar(
            select(UserDevice)
            .where(UserDevice.id == device_id)
            .where(UserDevice.status == "active")
        )

    async def get_active_sync_session_for_device_node(
        self, *, user_id: UUID, device_id: UUID, node_id: UUID
    ) -> SyncSession | None:
        """
        读取设备在指定节点上的有效同步会话

        :param user_id (UUID): 用户 ID
        :param device_id (UUID): 设备 ID
        :param node_id (UUID): 节点 ID

        :return SyncSession: 同步会话实体
        """

        return await self._session.scalar(
            select(SyncSession)
            .join(Workspace, Workspace.id == SyncSession.workspace_id)
            .where(SyncSession.user_id == user_id)
            .where(Workspace.device_id == device_id)
            .where(SyncSession.node_id == node_id)
            .where(SyncSession.status.in_(["starting", "active", "paused"]))
            .order_by(SyncSession.created_at.desc())
        )

    async def get_active_device_wireguard_peer(self, device_id: UUID) -> WireGuardPeer | None:
        """
        读取设备的活跃 WireGuard peer

        :param device_id (UUID): 设备 ID

        :return WireGuardPeer: WireGuard peer
        """

        return await self._session.scalar(
            select(WireGuardPeer)
            .where(WireGuardPeer.user_device_id == device_id)
            .where(WireGuardPeer.peer_type == "device")
            .where(WireGuardPeer.status == "active")
        )

    async def list_connectable_nodes(self) -> Sequence[Node]:
        """
        列出具备 WireGuard 和 SSH 连接信息的节点

        :return Sequence: 节点列表
        """

        result = await self._session.scalars(
            select(Node)
            .where(Node.status.in_(["healthy", "degraded", "maintenance"]))
            .where(Node.wireguard_ip.is_not(None))
            .where(Node.wireguard_public_key.is_not(None))
            .where(Node.wireguard_endpoint.is_not(None))
            .order_by(Node.weight.desc(), Node.created_at)
        )
        return result.all()

    async def list_active_ssh_keys_for_device(self, device_id: UUID) -> Sequence[SshKey]:
        """
        列出设备活跃 SSH 公钥

        :param device_id (UUID): 设备 ID

        :return Sequence: SSH 公钥列表
        """

        result = await self._session.scalars(
            select(SshKey)
            .where(SshKey.user_device_id == device_id)
            .where(SshKey.status == "active")
            .order_by(SshKey.created_at)
        )
        return result.all()

    async def get_session(self, session_id: UUID) -> Session | None:
        """
        读取工具 session

        :param session_id (UUID): session ID

        :return Session: session 实体
        """

        return await self._session.get(Session, session_id)

    async def get_tool_account(self, account_id: UUID) -> ToolAccount | None:
        """读取用于绑定 attach 校验的工具账户。"""

        return await self._session.get(ToolAccount, account_id)

    async def get_tool_account_profile(self, account_id: UUID) -> ToolAccountProfile | None:
        """读取用于绑定 attach 校验的工具账户 profile。"""

        return await self._session.scalar(
            select(ToolAccountProfile).where(ToolAccountProfile.tool_account_id == account_id)
        )

    async def get_node(self, node_id: UUID) -> Node | None:
        """
        读取节点

        :param node_id (UUID): 节点 ID

        :return Node: 节点实体
        """

        return await self._session.get(Node, node_id)

    async def get_blocking_sync_session(self, workspace_id: UUID) -> SyncSession | None:
        """
        读取阻止进入 session 的同步 session

        :param workspace_id (UUID): workspace ID

        :return SyncSession: 同步 session 实体
        """

        return await self._session.scalar(
            select(SyncSession)
            .where(SyncSession.workspace_id == workspace_id)
            .where(SyncSession.status != "stopped")
            .where(
                (SyncSession.conflict_status != "none")
                | SyncSession.status.in_(["conflicted", "failed"])
            )
            .order_by(SyncSession.created_at.desc())
        )
