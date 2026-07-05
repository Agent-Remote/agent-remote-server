from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import Node, NodeHeartbeat, NodeTask, NodeTaskResult


class NodeRepository:
    """
    节点和节点任务仓储
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_node(self, node: Node) -> Node:
        """
        新增节点

        :param node (Node): 节点实体

        :return Node: 节点实体
        """

        self._session.add(node)
        await self._session.flush()
        return node

    async def get_node(self, node_id: UUID) -> Node | None:
        """
        按 ID 读取节点

        :param node_id (UUID): 节点 ID

        :return Node: 节点实体
        """

        return await self._session.get(Node, node_id)

    async def get_node_by_token_hash(self, token_hash: str) -> Node | None:
        """
        按 node token 哈希读取节点

        :param token_hash (str): node token 哈希

        :return Node: 节点实体
        """

        return await self._session.scalar(select(Node).where(Node.node_token_hash == token_hash))

    async def list_nodes(self) -> Sequence[Node]:
        """
        列出节点

        :return Sequence: 节点列表
        """

        result = await self._session.scalars(select(Node).order_by(Node.created_at))
        return result.all()

    async def add_heartbeat(self, heartbeat: NodeHeartbeat) -> NodeHeartbeat:
        """
        新增节点心跳

        :param heartbeat (NodeHeartbeat): 心跳实体

        :return NodeHeartbeat: 心跳实体
        """

        self._session.add(heartbeat)
        await self._session.flush()
        return heartbeat

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

    async def list_tasks(
        self,
        *,
        status: str | None,
        limit: int,
    ) -> Sequence[NodeTask]:
        """
        列出节点任务

        :param status (str | None): 状态过滤
        :param limit (int): 最大返回数量

        :return Sequence: 节点任务列表
        """

        statement = select(NodeTask).order_by(NodeTask.created_at.desc()).limit(limit)
        if status is not None:
            statement = statement.where(NodeTask.status == status)
        result = await self._session.scalars(statement)
        return result.all()

    async def list_pollable_tasks(
        self,
        *,
        node_id: UUID,
        now: datetime,
        limit: int,
    ) -> Sequence[NodeTask]:
        """
        列出可租约任务

        :param node_id (UUID): 节点 ID
        :param now (datetime): 当前时间
        :param limit (int): 最大数量

        :return Sequence: 节点任务列表
        """

        normalized_now = now if now.tzinfo else now.replace(tzinfo=UTC)
        result = await self._session.scalars(
            select(NodeTask)
            .where(NodeTask.node_id == node_id)
            .where(
                (NodeTask.status == "pending")
                | ((NodeTask.status == "leased") & (NodeTask.lease_until <= normalized_now))
            )
            .order_by(NodeTask.created_at)
            .limit(limit)
        )
        return result.all()

    async def add_task_result(self, result: NodeTaskResult) -> NodeTaskResult:
        """
        新增任务结果

        :param result (NodeTaskResult): 任务结果实体

        :return NodeTaskResult: 任务结果实体
        """

        self._session.add(result)
        await self._session.flush()
        return result

    async def get_task_result(self, task_id: str) -> NodeTaskResult | None:
        """
        按 task_id 读取任务结果

        :param task_id (str): 任务 ID

        :return NodeTaskResult: 任务结果实体
        """

        return await self._session.scalar(
            select(NodeTaskResult).where(NodeTaskResult.task_id == task_id)
        )
