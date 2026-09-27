"""
查询发布目标并对目录和分支执行带 epoch 的原子 head 变更。
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, AccountSkillState


class SkillPublicationRepository:
    """
    发布服务持有用户存储锁，仓储仍用精确前置条件防止陈旧 head 写入。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定整个目录发布事务。

        :param session (AsyncSession): 外层异步事务
        """
        self._session = session

    async def receipt(self, user_id: UUID, finalization_id: UUID) -> SkillFinalization | None:
        """
        只返回本用户的原始收尾输入。

        :param user_id (UUID): 已授权用户
        :param finalization_id (UUID): 收尾身份
        :return SkillFinalization | None: 本用户收尾或空值
        """
        return await self._session.scalar(
            select(SkillFinalization)
            .where(
                SkillFinalization.user_id == user_id,
                SkillFinalization.id == finalization_id,
            )
            .execution_options(populate_existing=True)
        )

    async def snapshot(self, receipt: SkillFinalization) -> SessionSkillSnapshot:
        """
        从已授权收尾取得其原始精确快照。

        :param receipt (SkillFinalization): 已授权收尾
        :return SessionSkillSnapshot: 外键固定的原始快照
        """
        row = await self._session.scalar(
            select(SessionSkillSnapshot)
            .where(
                SessionSkillSnapshot.user_id == receipt.user_id,
                SessionSkillSnapshot.account_id == receipt.account_id,
                SessionSkillSnapshot.id == receipt.snapshot_id,
            )
            .execution_options(populate_existing=True)
        )
        assert row is not None
        return row

    async def latest(self, receipt: SkillFinalization) -> SkillPublication | None:
        """
        初次发布重试返回最后一次完整结果，不重新解释已冲突输入。

        :param receipt (SkillFinalization): 已授权收尾
        :return SkillPublication | None: 已有尝试或空值
        """
        return await self._session.scalar(
            select(SkillPublication)
            .where(
                SkillPublication.user_id == receipt.user_id,
                SkillPublication.account_id == receipt.account_id,
                SkillPublication.finalization_id == receipt.id,
            )
            .order_by(SkillPublication.attempt.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )

    async def attempt(self, user_id: UUID, publication_id: UUID) -> SkillPublication | None:
        """
        重新计算只能定位同用户的已保存尝试。

        :param user_id (UUID): 用户身份
        :param publication_id (UUID): 原尝试身份
        :return SkillPublication | None: 同用户尝试或空值
        """
        return await self._session.scalar(
            select(SkillPublication)
            .where(
                SkillPublication.user_id == user_id,
                SkillPublication.id == publication_id,
            )
            .execution_options(populate_existing=True)
        )

    async def branch(self, user_id: UUID, account_id: UUID, state_id: UUID) -> AccountSkillState:
        """
        返回原快照固定的分支，不按当前默认版本替换身份。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :param state_id (UUID): 原始分支身份
        :return AccountSkillState: 已授权的精确分支
        """
        row = await self._session.scalar(
            select(AccountSkillState)
            .where(
                AccountSkillState.user_id == user_id,
                AccountSkillState.account_id == account_id,
                AccountSkillState.id == state_id,
            )
            .execution_options(populate_existing=True)
        )
        assert row is not None
        return row

    async def local(
        self, user_id: UUID, account_id: UUID, skill_id: UUID
    ) -> AccountLocalSkill | None:
        """
        核对本地来源当前是否仍可接收该账户写入。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :param skill_id (UUID): 原始本地身份
        :return AccountLocalSkill | None: 已授权来源或空值
        """
        return await self._session.scalar(
            select(AccountLocalSkill)
            .where(
                AccountLocalSkill.user_id == user_id,
                AccountLocalSkill.account_id == account_id,
                AccountLocalSkill.id == skill_id,
            )
            .execution_options(populate_existing=True)
        )

    async def active_local_names(self, user_id: UUID, account_id: UUID) -> set[str]:
        """
        已停用但未移除的本地来源同样占用名称。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :return set[str]: 账户已发布身份名称集合
        """
        return set(
            await self._session.scalars(
                select(AccountLocalSkill.name).where(
                    AccountLocalSkill.user_id == user_id,
                    AccountLocalSkill.account_id == account_id,
                    AccountLocalSkill.status == "active",
                )
            )
        )

    async def advance_directory(
        self,
        directory: AccountSkillDirectoryState,
        checkpoint_id: UUID,
    ) -> bool:
        """
        目录 epoch 和预期 head 均匹配才推进完整结果。

        :param directory (AccountSkillDirectoryState): 发布前读取的目标
        :param checkpoint_id (UUID): 已完整保存的新目录
        :return bool: 是否成功交换 head
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
                .values(head_checkpoint_id=checkpoint_id)
                .returning(AccountSkillDirectoryState.account_id)
                .execution_options(synchronize_session=False)
            )
            is not None
        )

    async def advance_branch(self, branch: AccountSkillState, checkpoint_id: UUID) -> bool:
        """
        分支仅在原 head、epoch 和保留状态均匹配时更新。

        :param branch (AccountSkillState): 发布前读取的精确分支
        :param checkpoint_id (UUID): 新建同分支完整视图
        :return bool: 是否成功交换 head
        """
        return (
            await self._session.scalar(
                update(AccountSkillState)
                .where(
                    AccountSkillState.user_id == branch.user_id,
                    AccountSkillState.account_id == branch.account_id,
                    AccountSkillState.id == branch.id,
                    AccountSkillState.epoch == branch.epoch,
                    AccountSkillState.expired.is_(False),
                    AccountSkillState.head_checkpoint_id == branch.head_checkpoint_id,
                )
                .values(head_checkpoint_id=checkpoint_id)
                .returning(AccountSkillState.id)
                .execution_options(synchronize_session=False)
            )
            is not None
        )
