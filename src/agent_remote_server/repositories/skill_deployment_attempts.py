"""
按原用户与操作读取完整尝试链，所有追加复用调用方事务。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_deployment_attempts import (
    SkillDeploymentAttempt,
    SkillDeploymentRetry,
)


class SkillDeploymentAttemptRepository:
    """
    不从账户当前绑定猜测历史归属，也不提供覆盖原始尝试的接口。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        使用已取得用户存储锁的请求事务。

        :param session (AsyncSession): 当前用户事务
        """
        self.session = session

    async def attempts(
        self, user_id: UUID, operation_id: UUID
    ) -> tuple[SkillDeploymentAttempt, ...]:
        """
        返回完整原始序列供服务校验，缺失行不能解释为已完成。

        :param user_id (UUID): 已认证所有者
        :param operation_id (UUID): 原配置操作
        :return tuple[SkillDeploymentAttempt, ...]: 同一操作的全部尝试
        """
        return tuple(
            await self.session.scalars(
                select(SkillDeploymentAttempt)
                .where(
                    SkillDeploymentAttempt.user_id == user_id,
                    SkillDeploymentAttempt.operation_id == operation_id,
                )
                .order_by(SkillDeploymentAttempt.account_id, SkillDeploymentAttempt.number)
                .execution_options(populate_existing=True)
            )
        )

    async def retry_by_key(self, user_id: UUID, key: str) -> SkillDeploymentRetry | None:
        """
        同用户重试键独立于当前尝试状态，历史成功后仍可恢复原受理。

        :param user_id (UUID): 已认证所有者
        :param key (str): 原重试幂等键
        :return SkillDeploymentRetry | None: 已保存的重试身份
        """
        return await self.session.scalar(
            select(SkillDeploymentRetry).where(
                SkillDeploymentRetry.user_id == user_id,
                SkillDeploymentRetry.idempotency_key == key,
            )
        )
