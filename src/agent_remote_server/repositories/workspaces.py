from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import Node, NodeTask, SyncSession, UserDevice, Workspace


class WorkspaceRepository:
    """
    workspace 和同步 session 仓储
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_workspace(self, workspace: Workspace) -> Workspace:
        """
        新增 workspace

        :param workspace (Workspace): workspace 实体

        :return Workspace: workspace 实体
        """

        self._session.add(workspace)
        await self._session.flush()
        return workspace

    async def get_workspace(self, workspace_id: UUID) -> Workspace | None:
        """
        按 ID 读取 workspace

        :param workspace_id (UUID): workspace ID

        :return Workspace: workspace 实体
        """

        return await self._session.get(Workspace, workspace_id)

    async def get_workspace_by_project_key(
        self, *, user_id: UUID, project_key: str
    ) -> Workspace | None:
        """
        按项目 key 读取 workspace

        :param user_id (UUID): 用户 ID
        :param project_key (str): 项目 key

        :return Workspace: workspace 实体
        """

        return await self._session.scalar(
            select(Workspace)
            .where(Workspace.user_id == user_id)
            .where(Workspace.project_key == project_key)
            .order_by(Workspace.created_at)
        )

    async def list_workspaces_for_user(self, user_id: UUID) -> Sequence[Workspace]:
        """
        列出用户 workspace

        :param user_id (UUID): 用户 ID

        :return Sequence: workspace 列表
        """

        result = await self._session.scalars(
            select(Workspace).where(Workspace.user_id == user_id).order_by(Workspace.created_at)
        )
        return result.all()

    async def get_active_device(self, *, user_id: UUID, device_id: UUID) -> UserDevice | None:
        """
        读取用户 active 设备

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

    async def add_sync_session(self, sync_session: SyncSession) -> SyncSession:
        """
        新增同步 session

        :param sync_session (SyncSession): 同步 session 实体

        :return SyncSession: 同步 session 实体
        """

        self._session.add(sync_session)
        await self._session.flush()
        return sync_session

    async def get_sync_session(self, sync_session_id: UUID) -> SyncSession | None:
        """
        按 ID 读取同步 session

        :param sync_session_id (UUID): 同步 session ID

        :return SyncSession: 同步 session 实体
        """

        return await self._session.get(SyncSession, sync_session_id)

    async def get_current_sync_session_for_workspace(
        self, *, workspace_id: UUID
    ) -> SyncSession | None:
        """
        读取 workspace 当前同步 session

        :param workspace_id (UUID): workspace ID

        :return SyncSession: 同步 session 实体
        """

        return await self._session.scalar(
            select(SyncSession)
            .where(SyncSession.workspace_id == workspace_id)
            .where(SyncSession.status != "stopped")
            .order_by(SyncSession.created_at.desc())
        )

    async def list_sync_sessions_for_user(self, user_id: UUID) -> Sequence[SyncSession]:
        """
        列出用户同步 session

        :param user_id (UUID): 用户 ID

        :return Sequence: 同步 session 列表
        """

        result = await self._session.scalars(
            select(SyncSession)
            .where(SyncSession.user_id == user_id)
            .order_by(SyncSession.created_at)
        )
        return result.all()

    async def list_connectable_nodes(self) -> Sequence[Node]:
        """
        列出可承载同步的节点

        :return Sequence: 节点列表
        """

        result = await self._session.scalars(
            select(Node)
            .where(Node.status.in_(["healthy", "degraded"]))
            .where(Node.ssh_host.is_not(None) | Node.wireguard_ip.is_not(None))
            .order_by(Node.weight.desc(), Node.created_at)
        )
        return result.all()

    async def get_node(self, node_id: UUID) -> Node | None:
        """
        按 ID 读取节点

        :param node_id (UUID): 节点 ID

        :return Node: 节点实体
        """

        return await self._session.get(Node, node_id)

    async def add_task(self, task: NodeTask) -> NodeTask:
        """
        新增节点任务

        :param task (NodeTask): 节点任务实体

        :return NodeTask: 节点任务实体
        """

        self._session.add(task)
        await self._session.flush()
        return task

    async def get_task_by_task_id(self, task_id: str) -> NodeTask | None:
        """
        按 task_id 读取任务

        :param task_id (str): 任务 ID

        :return NodeTask: 节点任务实体
        """

        return await self._session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
