"""
访问经所有者授权的原始配置导入凭据及其事务绑定。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask, ToolAccount, ToolAccountConfigImportRequest


class ConfigImportRequestRepository:
    """
    只保存不可变受理身份，提交由服务统一负责。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        使用请求级异步事务。

        :param session (AsyncSession): 当前请求事务
        """
        self.session = session

    async def account(
        self, user_id: UUID, account_id: UUID, *, lock: bool = False
    ) -> ToolAccount | None:
        """
        每次查询都重新匹配所有者，并可与禁用操作串行化。

        :param user_id (UUID): 已认证用户身份
        :param account_id (UUID): 当前请求的精确账户
        :param lock (bool): 是否锁定用于新任务准入
        :return ToolAccount | None: 当前有权访问的账户
        """
        statement = select(ToolAccount).where(
            ToolAccount.id == account_id, ToolAccount.user_id == user_id
        )
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement.execution_options(populate_existing=True))

    async def request(
        self, user_id: UUID, request_id: UUID
    ) -> ToolAccountConfigImportRequest | None:
        """
        仅按用户及原调用方键定位，不查找最近任务。

        :param user_id (UUID): 已认证用户身份
        :param request_id (UUID): 原调用方请求身份
        :return ToolAccountConfigImportRequest | None: 同用户原始受理记录
        """
        return await self.session.scalar(
            select(ToolAccountConfigImportRequest)
            .where(
                ToolAccountConfigImportRequest.user_id == user_id,
                ToolAccountConfigImportRequest.request_id == request_id,
            )
            .execution_options(populate_existing=True)
        )

    async def task(self, task_id: str) -> NodeTask | None:
        """
        读取同事务刚创建的精确任务以记录数据库身份。

        :param task_id (str): 服务生成的逻辑任务身份
        :return NodeTask | None: 精确节点任务
        """
        return await self.session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))

    async def add(self, record: ToolAccountConfigImportRequest) -> None:
        """
        刷新唯一约束而不提前提交任务或凭据。

        :param record (ToolAccountConfigImportRequest): 不含原始文件正文的受理记录
        """
        self.session.add(record)
        await self.session.flush()
