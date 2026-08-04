from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from agent_remote_server.models import (
    DeviceSession,
    DeviceSessionApproval,
    Node,
    NodeTask,
    Session,
    UserDevice,
    Workspace,
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

    async def get_tool_session(
        self, tool_session_id: UUID, *, for_update: bool = False
    ) -> Session | None:
        """
        读取绑定的远端工具 session

        :param tool_session_id (UUID): 远端工具 session ID
        :param for_update (bool): 是否获取数据库行锁

        :return Session | None: 远端工具 session 实体
        """

        statement = select(Session).where(Session.id == tool_session_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def get_device(self, device_id: UUID, *, for_update: bool = False) -> UserDevice | None:
        """
        读取被控制设备

        :param device_id (UUID): 被控制设备 ID
        :param for_update (bool): 是否获取数据库行锁

        :return UserDevice | None: 用户设备实体
        """

        statement = select(UserDevice).where(UserDevice.id == device_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

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

    async def list_live_for_binding(
        self, *, tool_session_id: UUID, device_id: UUID, for_update: bool = False
    ) -> Sequence[DeviceSession]:
        """
        查询某个 Claude session 或本机设备当前占用的 live binding

        :param tool_session_id (UUID): 候选远端工具 session ID
        :param device_id (UUID): 当前认证设备 ID

        :return Sequence[DeviceSession]: 需要在 claim 事务中处理的 live 绑定
        """

        statement = (
            select(DeviceSession)
            .where(
                or_(
                    DeviceSession.tool_session_id == tool_session_id,
                    DeviceSession.device_id == device_id,
                )
            )
            .where(DeviceSession.status.not_in({"stopped", "denied", "expired", "failed"}))
            .order_by(DeviceSession.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return result.all()

    async def list_live_for_device(
        self, device_id: UUID, *, for_update: bool = False
    ) -> Sequence[DeviceSession]:
        """
        查询某个设备的全部 live 控制绑定

        :param device_id (UUID): 设备 ID
        :param for_update (bool): 是否获取数据库行锁

        :return Sequence[DeviceSession]: live 设备控制绑定
        """

        statement = (
            select(DeviceSession)
            .where(DeviceSession.device_id == device_id)
            .where(DeviceSession.status.not_in({"stopped", "denied", "expired", "failed"}))
            .order_by(DeviceSession.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return result.all()

    async def has_any_for_device(self, device_id: UUID) -> bool:
        """
        判断设备是否仍有受 retention 管理的控制绑定历史

        :param device_id (UUID): 设备 ID

        :return bool: 是否仍有受 retention 管理的控制绑定历史
        """

        return (
            await self._session.scalar(
                select(DeviceSession.id).where(DeviceSession.device_id == device_id).limit(1)
            )
            is not None
        )

    async def acquire_user_claim_lock(self, user_id: UUID) -> None:
        """
        在 PostgreSQL 中串行化同一用户的 claim/rebind 事务

        :param user_id (UUID): 当前设备所属用户 ID
        """

        if self._session.get_bind().dialect.name != "postgresql":
            return
        key = user_id.int & ((1 << 63) - 1)
        await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    async def list_live_for_tool_session(
        self, tool_session_id: UUID, *, for_update: bool = False
    ) -> Sequence[DeviceSession]:
        """
        查询某个远端 Claude session 的全部 live 设备绑定

        :param tool_session_id (UUID): 远端工具 session ID
        :param for_update (bool): 是否获取数据库行锁

        :return Sequence[DeviceSession]: live 设备控制绑定
        """

        statement = (
            select(DeviceSession)
            .where(DeviceSession.tool_session_id == tool_session_id)
            .where(DeviceSession.status.not_in({"stopped", "denied", "expired", "failed"}))
            .order_by(DeviceSession.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return result.all()

    async def list_due(self, now: datetime) -> Sequence[DeviceSession]:
        """
        查询已经超过最大 TTL 的 live 设备控制绑定并锁定

        :param now (datetime): 当前 UTC 时间

        :return Sequence[DeviceSession]: 待过期绑定
        """

        result = await self._session.scalars(
            select(DeviceSession)
            .where(DeviceSession.status.not_in({"stopped", "denied", "expired", "failed"}))
            .where(DeviceSession.expires_at <= now)
            .order_by(DeviceSession.expires_at.asc(), DeviceSession.id.asc())
            .with_for_update()
        )
        return result.all()

    async def lock_bindings(self, device_session_ids: Sequence[UUID]) -> Sequence[DeviceSession]:
        """
        按稳定顺序锁定 claim 涉及的所有 live binding

        :param device_session_ids (Sequence[UUID]): 待锁定的设备控制会话 ID

        :return Sequence[DeviceSession]: 已锁定的设备控制会话
        """

        if not device_session_ids:
            return []
        result = await self._session.scalars(
            select(DeviceSession)
            .where(DeviceSession.id.in_(set(device_session_ids)))
            .order_by(DeviceSession.id.asc())
            .with_for_update()
        )
        return result.all()

    async def list_candidate_rows(
        self, user_id: UUID
    ) -> Sequence[tuple[Session, Node, DeviceSession | None, UserDevice | None, Workspace]]:
        """
        查询用户可见的 Claude 控制候选及其当前 live 绑定

        :param user_id (UUID): 用户 ID

        :return Sequence[tuple]: Claude session、Node、live binding、设备和工作区
        """

        binding = aliased(DeviceSession)
        bound_device = aliased(UserDevice)
        result = await self._session.execute(
            select(Session, Node, binding, bound_device, Workspace)
            .join(Node, Node.id == Session.node_id)
            .join(Workspace, Workspace.id == Session.workspace_id)
            .outerjoin(
                binding,
                and_(
                    binding.tool_session_id == Session.id,
                    binding.status.not_in({"stopped", "denied", "expired", "failed"}),
                ),
            )
            .outerjoin(bound_device, bound_device.id == binding.device_id)
            .where(Session.user_id == user_id)
            .where(Session.tool_type == "claude")
            .where(Session.status.in_({"running", "active", "detached"}))
            .where(Session.device_control_protocol_version == 1)
            .order_by(Session.updated_at.desc(), Session.id.asc())
        )
        return result.tuples().all()

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
