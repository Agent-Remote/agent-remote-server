"""
读取不可变部署任务绑定，所有写入共享调用方用户事务。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import Node, NodeTask, NodeTaskResult, User
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_deployment_terminations import SkillDeploymentTermination
from agent_remote_server.models.skill_state import AccountSkillState


@dataclass(frozen=True)
class DeploymentTaskState:
    """
    引用扫描只读取有界身份与阶段，不加载任务私有 JSON 或改写 ORM 缓存。
    """

    id: UUID
    node_id: UUID
    task_id: str
    task_type: str
    status: str


class SkillDeploymentTaskRepository:
    """
    使用保存的任务身份发现归属，不信任可变任务载荷。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        保留外层事务。

        :param session (AsyncSession): 当前数据库事务
        """
        self.session = session

    async def bindings(
        self, user_id: UUID, operation_id: UUID, account_id: UUID
    ) -> tuple[SkillDeploymentTask, ...]:
        """
        完整读取原目标的已保存输入，不从当前规则推断缺失历史。

        :param user_id (UUID): 原所有者
        :param operation_id (UUID): 原配置操作
        :param account_id (UUID): 原目标账户
        :return tuple[SkillDeploymentTask, ...]: 全部精确尝试绑定
        """
        return tuple(
            await self.session.scalars(
                select(SkillDeploymentTask)
                .where(
                    SkillDeploymentTask.user_id == user_id,
                    SkillDeploymentTask.operation_id == operation_id,
                    SkillDeploymentTask.account_id == account_id,
                )
                .execution_options(populate_existing=True)
            )
        )

    async def for_task(self, task_id: UUID) -> SkillDeploymentTask | None:
        """
        持久绑定独立于任务类型，即使载荷被改写也不能退回通用授权。

        :param task_id (UUID): 精确任务数据库身份
        :return SkillDeploymentTask | None: 不授予权限的原绑定
        """
        return await self.session.scalar(
            select(SkillDeploymentTask)
            .where(SkillDeploymentTask.task_id == task_id)
            .execution_options(populate_existing=True)
        )

    async def node(self, node_id: UUID) -> Node | None:
        """
        刷新原节点能力，禁止沿用请求早期缓存的心跳。

        :param node_id (UUID): 固定节点身份
        :return Node | None: 当前节点报告
        """
        return await self.session.scalar(
            select(Node).where(Node.id == node_id).execution_options(populate_existing=True)
        )

    async def branch(
        self, user_id: UUID, account_id: UUID, state_id: UUID
    ) -> AccountSkillState | None:
        """
        刷新精确原分支，不能把新 head 当成旧输入的替代。

        :param user_id (UUID): 原所有者
        :param account_id (UUID): 原账户
        :param state_id (UUID): 原分支
        :return AccountSkillState | None: 当前纪元与来源身份
        """
        return await self.session.scalar(
            select(AccountSkillState)
            .where(
                AccountSkillState.user_id == user_id,
                AccountSkillState.account_id == account_id,
                AccountSkillState.id == state_id,
            )
            .execution_options(populate_existing=True)
        )

    async def owner_active(self, user_id: UUID) -> bool:
        """
        所有者停用后不继续为旧部署授予新内容请求。

        :param user_id (UUID): 原所有者
        :return bool: 当前是否仍为活动用户
        """
        return await self.session.scalar(select(User.status).where(User.id == user_id)) == "active"

    async def results(self, task: NodeTask) -> tuple[NodeTaskResult, ...]:
        """
        同时检查逻辑和数据库任务身份，重复结果不能被首行查询隐藏。

        :param task (NodeTask): 已按用户后任务锁序授权的任务
        :return tuple[NodeTaskResult, ...]: 至多两条候选，第二条即证明不一致
        """
        return tuple(
            await self.session.scalars(
                select(NodeTaskResult)
                .where(
                    or_(
                        NodeTaskResult.node_task_id == task.id,
                        NodeTaskResult.task_id == task.task_id,
                    )
                )
                .limit(2)
                .execution_options(populate_existing=True)
            )
        )

    async def retention_tasks(self, user_id: UUID, limit: int) -> tuple[DeploymentTaskState, ...]:
        """
        按真实绑定归属读取任务阶段，数量超限由完整索引调用方拒绝。

        :param user_id (UUID): 原所有者
        :param limit (int): 剩余扫描预算加一
        :return tuple[DeploymentTaskState, ...]: 有界任务身份和真实阶段
        """
        rows = await self.session.execute(
            select(
                NodeTask.id,
                NodeTask.node_id,
                NodeTask.task_id,
                NodeTask.task_type,
                NodeTask.status,
            )
            .join(SkillDeploymentTask, SkillDeploymentTask.task_id == NodeTask.id)
            .where(SkillDeploymentTask.user_id == user_id)
            .limit(limit)
        )
        return tuple(DeploymentTaskState(*row) for row in rows)

    async def termination(self, attempt_id: UUID) -> SkillDeploymentTermination | None:
        """
        在调用方的原用户锁内重读不可变撤权意图。

        :param attempt_id (UUID): 原始部署尝试
        :return SkillDeploymentTermination | None: 已保存的撤权意图
        """
        return await self.session.scalar(
            select(SkillDeploymentTermination)
            .where(SkillDeploymentTermination.attempt_id == attempt_id)
            .execution_options(populate_existing=True)
        )
