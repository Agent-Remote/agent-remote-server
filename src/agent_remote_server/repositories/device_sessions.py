from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import (
    DeviceSession,
    DeviceSessionApproval,
    Node,
    NodeTask,
    Session,
    UserDevice,
)


class DeviceSessionRepository:
    """
    本地设备控制会话仓储
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        初始化本地设备控制会话仓储

        :param session (AsyncSession): 异步数据库会话
        """

        self._session = session

    async def add(self, device_session: DeviceSession) -> DeviceSession:
        """
        新增本地设备控制会话

        :param device_session (DeviceSession): 设备控制会话实体

        :return DeviceSession: 已持久化的设备控制会话实体
        """

        self._session.add(device_session)
        await self._session.flush()
        return device_session

    async def get(
        self, device_session_id: UUID, *, for_update: bool = False
    ) -> DeviceSession | None:
        """
        按 ID 读取本地设备控制会话

        :param device_session_id (UUID): 设备控制会话 ID
        :param for_update (bool): 是否获取数据库行锁

        :return DeviceSession | None: 设备控制会话实体
        """

        statement = select(DeviceSession).where(DeviceSession.id == device_session_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def get_tool_session(self, tool_session_id: UUID) -> Session | None:
        """
        读取绑定的远端工具 session

        :param tool_session_id (UUID): 远端工具 session ID

        :return Session | None: 远端工具 session 实体
        """

        return await self._session.get(Session, tool_session_id)

    async def get_device(self, device_id: UUID) -> UserDevice | None:
        """
        读取被控制设备

        :param device_id (UUID): 被控制设备 ID

        :return UserDevice | None: 用户设备实体
        """

        return await self._session.get(UserDevice, device_id)

    async def get_node(self, node_id: UUID) -> Node | None:
        """
        读取远端工具 session 所在节点

        :param node_id (UUID): 远端节点 ID

        :return Node | None: 远端节点实体
        """

        return await self._session.get(Node, node_id)

    async def list_for_user(self, user_id: UUID) -> Sequence[DeviceSession]:
        """
        列出用户的设备控制会话

        :param user_id (UUID): 所属用户 ID

        :return Sequence[DeviceSession]: 设备控制会话列表
        """

        result = await self._session.scalars(
            select(DeviceSession)
            .where(DeviceSession.user_id == user_id)
            .order_by(DeviceSession.created_at.desc())
        )
        return result.all()

    async def list_for_device(self, device_id: UUID) -> Sequence[DeviceSession]:
        """
        列出绑定设备当前可处理的控制会话

        :param device_id (UUID): 当前认证设备 ID

        :return Sequence[DeviceSession]: 按创建时间正序排列的非终态设备控制会话
        """

        result = await self._session.scalars(
            select(DeviceSession)
            .where(DeviceSession.device_id == device_id)
            .where(DeviceSession.status.in_({"pending_device", "pending_user_approval", "active"}))
            .order_by(DeviceSession.created_at.asc())
        )
        return result.all()

    async def list_all(self) -> Sequence[DeviceSession]:
        """
        列出全部设备控制会话

        :return Sequence[DeviceSession]: 按创建时间倒序排列的设备控制会话列表
        """

        result = await self._session.scalars(
            select(DeviceSession).order_by(DeviceSession.created_at.desc())
        )
        return result.all()

    async def find_machine_lock(
        self, device_id: UUID, *, excluding_session_id: UUID
    ) -> DeviceSession | None:
        """
        读取设备当前由其他 session 持有的机器锁

        :param device_id (UUID): 被控制设备 ID
        :param excluding_session_id (UUID): 当前设备控制会话 ID

        :return DeviceSession | None: 持有机器锁的其他设备控制会话
        """

        return await self._session.scalar(
            select(DeviceSession)
            .where(DeviceSession.device_id == device_id)
            .where(DeviceSession.id != excluding_session_id)
            .where(DeviceSession.status == "active")
            .where(DeviceSession.lock_acquired_at.is_not(None))
            .with_for_update()
        )

    async def replace_approvals(
        self,
        device_session_id: UUID,
        approvals: list[DeviceSessionApproval],
    ) -> None:
        """
        保存当前 session 的本机应用审批摘要

        :param device_session_id (UUID): 设备控制会话 ID
        :param approvals (list[DeviceSessionApproval]): 本机应用审批摘要列表
        """

        existing = await self._session.scalars(
            select(DeviceSessionApproval).where(
                DeviceSessionApproval.device_session_id == device_session_id
            )
        )
        for approval in existing.all():
            await self._session.delete(approval)
        await self._session.flush()
        self._session.add_all(approvals)
        await self._session.flush()

    async def add_task(self, task: NodeTask) -> NodeTask:
        """
        新增设备控制节点任务

        :param task (NodeTask): 设备控制节点任务实体

        :return NodeTask: 已持久化的设备控制节点任务实体
        """

        self._session.add(task)
        await self._session.flush()
        return task

    async def delete_terminal_before(self, cutoff: datetime, *, limit: int) -> int:
        """
        删除保留期外的终态设备控制会话及审批摘要

        :param cutoff (datetime): 停止时间截止点
        :param limit (int): 单次最大删除数量

        :return int: 已删除会话数量
        """

        result = await self._session.scalars(
            select(DeviceSession)
            .where(DeviceSession.status.in_({"stopped", "denied", "expired", "failed"}))
            .where(DeviceSession.stopped_at.is_not(None))
            .where(DeviceSession.stopped_at < cutoff)
            .order_by(DeviceSession.stopped_at.asc(), DeviceSession.id.asc())
            .limit(limit)
        )
        sessions = list(result.all())
        if not sessions:
            return 0
        session_ids = [device_session.id for device_session in sessions]
        await self._session.execute(
            delete(DeviceSessionApproval).where(
                DeviceSessionApproval.device_session_id.in_(session_ids)
            )
        )
        for device_session in sessions:
            await self._session.delete(device_session)
        await self._session.flush()
        return len(sessions)
