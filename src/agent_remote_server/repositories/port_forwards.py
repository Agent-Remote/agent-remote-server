from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import (
    Node,
    PortForward,
    Session,
    SshKey,
    ToolAccount,
    User,
    UserDevice,
)

NON_TERMINAL_FORWARD_STATUSES = ("pending", "active", "disconnected")


class PortForwardRepository:
    """
    Session 端口转发仓储
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, port_forward: PortForward) -> PortForward:
        """
        新增端口转发

        :param port_forward (PortForward): 端口转发实体

        :return PortForward: 已新增实体
        """

        self._session.add(port_forward)
        await self._session.flush()
        return port_forward

    async def get(self, forward_id: UUID) -> PortForward | None:
        """
        按 ID 读取端口转发

        :param forward_id (UUID): 端口转发 ID

        :return PortForward: 端口转发实体
        """

        return await self._session.get(PortForward, forward_id)

    async def get_for_update(self, forward_id: UUID) -> PortForward | None:
        """
        加锁读取端口转发

        :param forward_id (UUID): 端口转发 ID

        :return PortForward: 端口转发实体
        """

        return await self._session.scalar(
            select(PortForward).where(PortForward.id == forward_id).with_for_update()
        )

    async def list_for_user(self, user_id: UUID) -> Sequence[PortForward]:
        """
        列出用户端口转发

        :param user_id (UUID): 用户 ID

        :return Sequence: 端口转发列表
        """

        result = await self._session.scalars(
            select(PortForward)
            .where(PortForward.user_id == user_id)
            .order_by(PortForward.created_at.desc())
        )
        return result.all()

    async def list_all(self) -> Sequence[PortForward]:
        """
        列出全部端口转发

        :return Sequence: 端口转发列表
        """

        result = await self._session.scalars(
            select(PortForward).order_by(PortForward.created_at.desc())
        )
        return result.all()

    async def list_non_terminal_ids(
        self, limit: int, *, after_id: UUID | None = None
    ) -> Sequence[UUID]:
        """
        列出待对账的非终态转发 ID

        :param limit (int): 最大批量数

        :return Sequence: 转发 ID
        """

        statement = select(PortForward.id).where(
            PortForward.status.in_(NON_TERMINAL_FORWARD_STATUSES)
        )
        if after_id is not None:
            statement = statement.where(PortForward.id > after_id)
        result = await self._session.scalars(statement.order_by(PortForward.id).limit(limit))
        return result.all()

    async def count_active(
        self,
        *,
        user_id: UUID | None = None,
        device_id: UUID | None = None,
        session_id: UUID | None = None,
    ) -> int:
        """
        统计指定范围的非终态转发数

        :param user_id (UUID): 用户 ID
        :param device_id (UUID): 设备 ID
        :param session_id (UUID): 工具会话 ID

        :return int: 转发数量
        """

        statement = select(func.count(PortForward.id)).where(
            PortForward.status.in_(NON_TERMINAL_FORWARD_STATUSES)
        )
        if user_id is not None:
            statement = statement.where(PortForward.user_id == user_id)
        if device_id is not None:
            statement = statement.where(PortForward.device_id == device_id)
        if session_id is not None:
            statement = statement.where(PortForward.session_id == session_id)
        return int((await self._session.scalar(statement)) or 0)

    async def get_session(self, session_id: UUID) -> Session | None:
        """
        读取工具 session

        :param session_id (UUID): 工具会话 ID

        :return Session: session 实体
        """

        return await self._session.get(Session, session_id)

    async def get_node(self, node_id: UUID) -> Node | None:
        """
        读取节点

        :param node_id (UUID): 节点 ID

        :return Node: 节点实体
        """

        return await self._session.get(Node, node_id)

    async def get_active_user(self, user_id: UUID) -> User | None:
        """
        读取活跃用户

        :param user_id (UUID): 用户 ID

        :return User: 活跃用户
        """

        return await self._session.scalar(
            select(User).where(User.id == user_id).where(User.status == "active")
        )

    async def get_active_tool_account(self, tool_account_id: UUID) -> ToolAccount | None:
        """
        读取活跃工具账户

        :param tool_account_id (UUID): 工具账户 ID

        :return ToolAccount: 活跃工具账户
        """

        return await self._session.scalar(
            select(ToolAccount)
            .where(ToolAccount.id == tool_account_id)
            .where(ToolAccount.status == "active")
        )

    async def get_active_device(self, *, user_id: UUID, device_id: UUID) -> UserDevice | None:
        """
        读取用户的活跃设备

        :param user_id (UUID): 用户 ID
        :param device_id (UUID): 设备 ID

        :return UserDevice: 活跃设备
        """

        return await self._session.scalar(
            select(UserDevice)
            .where(UserDevice.id == device_id)
            .where(UserDevice.user_id == user_id)
            .where(UserDevice.status == "active")
        )

    async def lock_user(self, user_id: UUID) -> User | None:
        """
        加锁读取用户以串行化配额检查

        :param user_id (UUID): 用户 ID

        :return User: 用户实体
        """

        return await self._session.scalar(select(User).where(User.id == user_id).with_for_update())

    async def get_active_ssh_key(self, *, device_id: UUID, ssh_key_id: UUID) -> SshKey | None:
        """
        读取设备指定的活跃 SSH key

        :param device_id (UUID): 设备 ID
        :param ssh_key_id (UUID): SSH 密钥 ID

        :return SshKey: 活跃 SSH key
        """

        return await self._session.scalar(
            select(SshKey)
            .where(SshKey.id == ssh_key_id)
            .where(SshKey.user_device_id == device_id)
            .where(SshKey.status == "active")
        )

    async def first_active_ssh_key(self, device_id: UUID) -> SshKey | None:
        """
        读取设备首个活跃 SSH key

        :param device_id (UUID): 设备 ID

        :return SshKey: 活跃 SSH key
        """

        return await self._session.scalar(
            select(SshKey)
            .where(SshKey.user_device_id == device_id)
            .where(SshKey.status == "active")
            .order_by(SshKey.created_at)
        )
