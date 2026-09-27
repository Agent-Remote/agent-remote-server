"""
按所有者和稳定来源读取迁移冲突及原分支，不初始化任何状态。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import AccountSkillState


class SkillMigrationConflictRepository:
    """
    所有读取共享外层用户内容锁，分页不因状态更新时间而重新排序。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定异步请求事务。

        :param session (AsyncSession): 请求事务
        """
        self._session = session

    async def record(self, user_id: UUID, migration_id: UUID) -> SkillBranchPreparation | None:
        """
        记录身份始终在认证用户内授权。

        :param user_id (UUID): 当前用户
        :param migration_id (UUID): 原受理身份
        :return SkillBranchPreparation | None: 同用户记录或不存在
        """
        return await self._session.scalar(
            select(SkillBranchPreparation)
            .where(
                SkillBranchPreparation.user_id == user_id,
                SkillBranchPreparation.id == migration_id,
            )
            .execution_options(populate_existing=True)
        )

    async def conflicts(
        self,
        user_id: UUID,
        account_id: UUID,
        skill_id: UUID | None,
        limit: int,
        before: SkillBranchPreparation | None,
    ) -> Sequence[SkillBranchPreparation]:
        """
        同时间记录按身份稳定分页，只返回未解决或已失效冲突。

        :param user_id (UUID): 已认证用户
        :param account_id (UUID): 已授权账户
        :param skill_id (UUID | None): 明确稳定来源或全部来源
        :param limit (int): 有界读取数量
        :param before (SkillBranchPreparation | None): 已授权上一页末尾记录
        :return Sequence[SkillBranchPreparation]: 稳定顺序的原始记录
        """
        query = select(SkillBranchPreparation).where(
            SkillBranchPreparation.user_id == user_id,
            SkillBranchPreparation.account_id == account_id,
            SkillBranchPreparation.status.in_(("conflicted", "superseded")),
        )
        if skill_id is not None:
            query = query.where(SkillBranchPreparation.installation_id == skill_id)
        if before is not None:
            query = query.where(
                or_(
                    SkillBranchPreparation.created_at < before.created_at,
                    and_(
                        SkillBranchPreparation.created_at == before.created_at,
                        SkillBranchPreparation.id < before.id,
                    ),
                )
            )
        return (
            await self._session.scalars(
                query.order_by(
                    SkillBranchPreparation.created_at.desc(), SkillBranchPreparation.id.desc()
                )
                .limit(limit)
                .execution_options(populate_existing=True)
            )
        ).all()

    async def branch(self, row: SkillBranchPreparation, state_id: UUID) -> AccountSkillState:
        """
        原始分支按完整保存范围读取，移除或重新安装不改选来源。

        :param row (SkillBranchPreparation): 已授权原始记录
        :param state_id (UUID): 保存的分支身份
        :return AccountSkillState: 原始来源或目标分支
        """
        branch = await self._session.scalar(
            select(AccountSkillState)
            .where(
                AccountSkillState.user_id == row.user_id,
                AccountSkillState.account_id == row.account_id,
                AccountSkillState.installation_id == row.installation_id,
                AccountSkillState.installation_epoch == row.installation_epoch,
                AccountSkillState.id == state_id,
            )
            .execution_options(populate_existing=True)
        )
        assert branch is not None
        return branch
