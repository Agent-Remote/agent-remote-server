from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import BrowserSession, Node, NodeTask, ToolAccount


class BrowserSessionRepository:
    """
    远端临时浏览器 session 仓储
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_browser_session(self, browser_session: BrowserSession) -> BrowserSession:
        """
        新增浏览器 session

        :param browser_session (BrowserSession): 浏览器 session 实体
        :return BrowserSession: 浏览器 session 实体
        """

        self._session.add(browser_session)
        await self._session.flush()
        return browser_session

    async def get_browser_session(self, browser_session_id: UUID) -> BrowserSession | None:
        """
        按 ID 读取浏览器 session

        :param browser_session_id (UUID): 浏览器 session ID
        :return BrowserSession: 浏览器 session 实体
        """

        return await self._session.get(BrowserSession, browser_session_id)

    async def delete_browser_session(self, browser_session: BrowserSession) -> None:
        """
        删除终态浏览器 session

        :param browser_session (BrowserSession): 浏览器 session 实体
        """

        await self._session.delete(browser_session)

    async def list_browser_sessions_for_user(self, user_id: UUID) -> Sequence[BrowserSession]:
        """
        列出用户浏览器 session

        :param user_id (UUID): 用户 ID
        :return Sequence: 浏览器 session 列表
        """

        result = await self._session.scalars(
            select(BrowserSession)
            .where(BrowserSession.user_id == user_id)
            .order_by(BrowserSession.created_at.desc())
        )
        return result.all()

    async def list_expired_active_sessions(self, now: datetime) -> Sequence[BrowserSession]:
        """
        列出已过期但未终止的浏览器 session

        :param now (datetime): 当前时间
        :return Sequence: 浏览器 session 列表
        """

        normalized_now = now if now.tzinfo else now.replace(tzinfo=UTC)
        result = await self._session.scalars(
            select(BrowserSession)
            .where(BrowserSession.status.in_(["starting", "ready"]))
            .where(BrowserSession.expires_at <= normalized_now)
            .order_by(BrowserSession.expires_at)
        )
        return result.all()

    async def get_account(self, account_id: UUID) -> ToolAccount | None:
        """
        读取工具账户

        :param account_id (UUID): 工具账户 ID
        :return ToolAccount: 工具账户实体
        """

        return await self._session.get(ToolAccount, account_id)

    async def get_node(self, node_id: UUID) -> Node | None:
        """
        读取节点

        :param node_id (UUID): 节点 ID
        :return Node: 节点实体
        """

        return await self._session.get(Node, node_id)

    async def list_candidate_nodes(
        self, *, region_code: str, preferred_tags: list[str]
    ) -> Sequence[Node]:
        """
        列出可承载浏览器 session 的候选节点

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
