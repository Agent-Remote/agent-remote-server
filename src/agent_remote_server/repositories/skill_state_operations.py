"""
对状态重置恢复执行精确纪元交换并保存独立幂等结果。
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, AccountSkillState
from agent_remote_server.models.skill_state_operations import SkillStateOperation


class SkillStateOperationRepository:
    """
    所有变更均由已取得用户存储锁的外层事务提交。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        保存外层数据库事务。

        :param session (AsyncSession): 异步事务
        """
        self._session = session

    async def operation(self, user_id: UUID, key: str) -> SkillStateOperation | None:
        """
        幂等键只能重放其原始用户的状态操作。

        :param user_id (UUID): 当前用户
        :param key (str): 原命令键
        :return SkillStateOperation | None: 原始回执或空值
        """
        return await self._session.scalar(
            select(SkillStateOperation).where(
                SkillStateOperation.user_id == user_id, SkillStateOperation.idempotency_key == key
            )
        )

    async def operation_by_id(
        self, user_id: UUID, operation_id: UUID
    ) -> SkillStateOperation | None:
        """
        只读取当前用户原始状态回执，不凭操作身份扩大授权。

        :param user_id (UUID): 当前用户
        :param operation_id (UUID): 原始状态操作身份
        :return SkillStateOperation | None: 当前用户回执或空值
        """
        return await self._session.scalar(
            select(SkillStateOperation).where(
                SkillStateOperation.user_id == user_id, SkillStateOperation.id == operation_id
            )
        )

    async def advance_branch(self, branch: AccountSkillState, checkpoint_id: UUID) -> bool:
        """
        显式恢复可解除过期状态，但必须匹配原始 head、纪元和过期值。

        :param branch (AccountSkillState): 已锁定旧分支
        :param checkpoint_id (UUID): 新检查点
        :return bool: 是否成功交换并推进纪元
        """
        return (
            await self._session.scalar(
                update(AccountSkillState)
                .where(
                    AccountSkillState.user_id == branch.user_id,
                    AccountSkillState.account_id == branch.account_id,
                    AccountSkillState.id == branch.id,
                    AccountSkillState.epoch == branch.epoch,
                    AccountSkillState.head_checkpoint_id == branch.head_checkpoint_id,
                    AccountSkillState.expired == branch.expired,
                )
                .values(head_checkpoint_id=checkpoint_id, epoch=branch.epoch + 1, expired=False)
                .returning(AccountSkillState.id)
                .execution_options(synchronize_session=False)
            )
            is not None
        )

    async def advance_directory(
        self, directory: AccountSkillDirectoryState, checkpoint_id: UUID, advance_epoch: bool
    ) -> bool:
        """
        单项只推进目录 head，完整目录操作同时推进其纪元。

        :param directory (AccountSkillDirectoryState): 已锁定旧目录
        :param checkpoint_id (UUID): 新目录检查点
        :param advance_epoch (bool): 是否推进目录纪元
        :return bool: 是否成功交换
        """
        return (
            await self._session.scalar(
                update(AccountSkillDirectoryState)
                .where(
                    AccountSkillDirectoryState.user_id == directory.user_id,
                    AccountSkillDirectoryState.account_id == directory.account_id,
                    AccountSkillDirectoryState.mode == "managed_v1",
                    AccountSkillDirectoryState.epoch == directory.epoch,
                    AccountSkillDirectoryState.head_checkpoint_id == directory.head_checkpoint_id,
                )
                .values(
                    head_checkpoint_id=checkpoint_id, epoch=directory.epoch + int(advance_epoch)
                )
                .returning(AccountSkillDirectoryState.account_id)
                .execution_options(synchronize_session=False)
            )
            is not None
        )

    async def supersede_conflicts(self, user_id: UUID, account_id: UUID, action: str) -> int:
        """
        新 head 使旧计划失效，但不在重置恢复命令里自动执行旧提交。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 已变更账户
        :param action (str): 重置或恢复原因
        :return int: 已取代的未解决尝试数
        """
        result = await self._session.scalars(
            update(SkillPublication)
            .where(
                SkillPublication.user_id == user_id,
                SkillPublication.account_id == account_id,
                SkillPublication.status == "conflicted",
            )
            .values(status="superseded", reason="state_" + action)
            .returning(SkillPublication.id)
            .execution_options(synchronize_session=False)
        )
        return len(result.all())
