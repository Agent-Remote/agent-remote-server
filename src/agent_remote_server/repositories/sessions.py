from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import (
    DeveloperCredentialProfile,
    Node,
    NodeTask,
    Session,
    ToolAccount,
    ToolAccountDeveloperCredentialProfile,
    ToolAccountProfile,
    Workspace,
)


class SessionRepository:
    """
    工具运行 session 仓储
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_session(self, tool_session: Session) -> Session:
        """
        新增工具 session

        :param tool_session (Session): 工具 session 实体

        :return Session: 工具 session 实体
        """

        self._session.add(tool_session)
        await self._session.flush()
        return tool_session

    async def get_session(self, session_id: UUID) -> Session | None:
        """
        按 ID 读取工具 session

        :param session_id (UUID): 工具 session ID

        :return Session: 工具 session 实体
        """

        return await self._session.get(Session, session_id)

    async def list_sessions_for_user(
        self, user_id: UUID, tool_type: str | None
    ) -> Sequence[Session]:
        """
        列出用户工具 session

        :param user_id (UUID): 用户 ID
        :param tool_type (str): 工具类型

        :return Sequence: 工具 session 列表
        """

        statement = select(Session).where(Session.user_id == user_id)
        if tool_type is not None:
            statement = statement.where(Session.tool_type == tool_type)
        result = await self._session.scalars(statement.order_by(Session.updated_at.desc()))
        return result.all()

    async def get_latest_project_session(
        self, *, user_id: UUID, tool_type: str, project_key: str
    ) -> Session | None:
        """
        读取当前项目最近可恢复 session

        :param user_id (UUID): 用户 ID
        :param tool_type (str): 工具类型
        :param project_key (str): 项目 key

        :return Session: 工具 session 实体
        """

        return await self._session.scalar(
            select(Session)
            .where(Session.user_id == user_id)
            .where(Session.tool_type == tool_type)
            .where(Session.project_key == project_key)
            .where(Session.status.in_(["starting", "running", "active"]))
            .order_by(Session.updated_at.desc(), Session.created_at.desc())
        )

    async def list_active_sessions_for_account(self, account_id: UUID) -> Sequence[Session]:
        """
        列出同工具账户活跃 session

        :param account_id (UUID): 工具账户 ID
        :return Sequence: 工具 session 列表
        """

        result = await self._session.scalars(
            select(Session)
            .where(Session.tool_account_id == account_id)
            .where(Session.status.in_(["starting", "running", "active"]))
            .order_by(Session.created_at)
        )
        return result.all()

    async def get_account(self, account_id: UUID) -> ToolAccount | None:
        """
        读取工具账户

        :param account_id (UUID): 工具账户 ID
        :return ToolAccount: 工具账户实体
        """

        return await self._session.get(ToolAccount, account_id)

    async def get_account_profile(self, account_id: UUID) -> ToolAccountProfile | None:
        """
        读取工具账户 profile

        :param account_id (UUID): 工具账户 ID
        :return ToolAccountProfile: profile 实体
        """

        return await self._session.scalar(
            select(ToolAccountProfile).where(ToolAccountProfile.tool_account_id == account_id)
        )

    async def get_developer_credential_profile_for_account(
        self, account_id: UUID
    ) -> DeveloperCredentialProfile | None:
        """
        读取工具账户绑定的开发凭据 profile
        """

        return await self._session.scalar(
            select(DeveloperCredentialProfile)
            .join(
                ToolAccountDeveloperCredentialProfile,
                ToolAccountDeveloperCredentialProfile.developer_credential_profile_id
                == DeveloperCredentialProfile.id,
            )
            .where(ToolAccountDeveloperCredentialProfile.tool_account_id == account_id)
        )

    async def get_workspace(self, workspace_id: UUID) -> Workspace | None:
        """
        读取 workspace

        :param workspace_id (UUID): workspace ID
        :return Workspace: workspace 实体
        """

        return await self._session.get(Workspace, workspace_id)

    async def get_node(self, node_id: UUID) -> Node | None:
        """
        读取节点

        :param node_id (UUID): 节点 ID
        :return Node: 节点实体
        """

        return await self._session.get(Node, node_id)

    async def list_candidate_nodes(
        self, *, tool_type: str, region_code: str, preferred_tags: list[str]
    ) -> Sequence[Node]:
        """
        列出可承载工具 session 的候选节点

        :param tool_type (str): 工具类型
        :param region_code (str): 地区代码
        :param preferred_tags (list): 偏好标签
        :return Sequence: 节点列表
        """

        result = await self._session.scalars(
            select(Node)
            .where(Node.status.in_(["healthy", "degraded"]))
            .order_by(Node.weight.desc(), Node.created_at)
        )
        nodes = []
        for node in result.all():
            if tool_type not in node.supported_tool_types:
                continue
            if node.region_code != region_code:
                continue
            if preferred_tags and not set(preferred_tags).issubset(set(node.tags)):
                continue
            nodes.append(node)
        return nodes

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
