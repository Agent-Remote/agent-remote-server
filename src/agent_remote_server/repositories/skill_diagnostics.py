"""
在调用方持有用户存储锁时汇总物理删除，不把 SQL 带入服务层。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion


@dataclass(frozen=True)
class DeletionUsage:
    """
    用户级物理任务计量与逻辑配额保持独立。
    """

    pending_tasks: int
    retrying_tasks: int
    completed_tasks: int
    pending_file_bytes: int
    cumulative_deleted_bytes: int


class SkillDiagnosticRepository:
    """
    汇总固定数量字段，避免载入用户全部历史删除任务。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定已有用户锁的请求事务。

        :param session (AsyncSession): 调用方事务
        """
        self.session = session

    async def deletion_usage(self, user_id: UUID) -> DeletionUsage:
        """
        同摘要不同生命周期的完成量可累加，待删除量不按类别重复计算。

        :param user_id (UUID): 已认证当前用户
        :return DeletionUsage: 不含内容摘要或路径的固定大小汇总
        """
        task = SkillContentDeletion
        pending = task.status == "pending"
        complete = task.status == "complete"
        row = (
            await self.session.execute(
                select(
                    func.coalesce(func.sum(case((pending, 1), else_=0)), 0),
                    func.coalesce(func.sum(case((pending & (task.attempts > 0), 1), else_=0)), 0),
                    func.coalesce(func.sum(case((complete, 1), else_=0)), 0),
                    func.coalesce(func.sum(case((pending, task.size), else_=0)), 0),
                    func.coalesce(func.sum(case((complete, task.size), else_=0)), 0),
                ).where(task.user_id == user_id)
            )
        ).one()
        return DeletionUsage(*(int(value) for value in row))
