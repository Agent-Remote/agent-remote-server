"""
读取配置导入的精确任务和活动所有者，避免账户亲和性扩展任务权限。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask, ToolAccount, User


class SkillImportRepository:
    """
    模式校验使用用户内容锁，所有查询刷新已有身份缓存。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        保存外层请求事务。

        :param session (AsyncSession): 异步数据库事务
        """
        self.session = session

    async def task(self, node_id: UUID, task_id: str) -> NodeTask | None:
        """
        精确读取当前节点的配置导入任务。

        :param node_id (UUID): 认证节点
        :param task_id (str): 任务外部身份
        :return NodeTask | None: 已授权类型的任务
        """
        return await self.session.scalar(
            select(NodeTask)
            .where(
                NodeTask.node_id == node_id,
                NodeTask.task_id == task_id,
                NodeTask.task_type == "import_tool_account_config",
            )
            .execution_options(populate_existing=True)
        )

    async def account(self, user_id: UUID, account_id: UUID) -> ToolAccount | None:
        """
        仅允许仍属于活动用户的原账户，不使用任务中的宿主路径证明归属。

        :param user_id (UUID): 任务所有者
        :param account_id (UUID): 任务账户
        :return ToolAccount | None: 同一活动用户的账户
        """
        return await self.session.scalar(
            select(ToolAccount)
            .join(User, User.id == ToolAccount.user_id)
            .where(ToolAccount.id == account_id, User.id == user_id, User.status == "active")
            .execution_options(populate_existing=True)
        )
