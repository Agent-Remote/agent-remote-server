from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import Node, NodeTask, ToolAccount, ToolAccountProfile


class ToolAccountRepository:
    """
    工具账户仓储
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_account(self, account: ToolAccount) -> ToolAccount:
        """
        新增工具账户

        :param account (ToolAccount): 工具账户实体

        :return ToolAccount: 工具账户实体
        """

        self._session.add(account)
        await self._session.flush()
        return account

    async def get_account(self, account_id: UUID) -> ToolAccount | None:
        """
        按 ID 读取工具账户

        :param account_id (UUID): 工具账户 ID

        :return ToolAccount: 工具账户实体
        """

        return await self._session.get(ToolAccount, account_id)

    async def list_accounts_for_user(self, user_id: UUID) -> Sequence[ToolAccount]:
        """
        列出用户工具账户

        :param user_id (UUID): 用户 ID

        :return Sequence: 工具账户列表
        """

        result = await self._session.scalars(
            select(ToolAccount)
            .where(ToolAccount.user_id == user_id)
            .order_by(ToolAccount.created_at)
        )
        return result.all()

    async def add_profile(self, profile: ToolAccountProfile) -> ToolAccountProfile:
        """
        新增工具账户 profile

        :param profile (ToolAccountProfile): profile 实体

        :return ToolAccountProfile: profile 实体
        """

        self._session.add(profile)
        await self._session.flush()
        return profile

    async def get_profile(self, account_id: UUID) -> ToolAccountProfile | None:
        """
        读取工具账户 profile

        :param account_id (UUID): 工具账户 ID

        :return ToolAccountProfile: profile 实体
        """

        return await self._session.scalar(
            select(ToolAccountProfile).where(ToolAccountProfile.tool_account_id == account_id)
        )

    async def get_node(self, node_id: UUID) -> Node | None:
        """
        按 ID 读取节点

        :param node_id (UUID): 节点 ID

        :return Node: 节点实体
        """

        return await self._session.get(Node, node_id)

    async def list_candidate_nodes(
        self,
        *,
        tool_type: str,
        region_code: str,
        preferred_tags: list[str],
    ) -> Sequence[Node]:
        """
        列出候选节点

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
        按 task_id 读取节点任务

        :param task_id (str): 任务 ID

        :return NodeTask: 节点任务实体
        """

        return await self._session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
