"""
限定账户本地身份、初始版本及运行分支的查询范围。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_local import AccountLocalSkill, AccountLocalSkillRevision
from agent_remote_server.models.skill_state import AccountSkillState


class SkillLocalRepository:
    """
    本地来源不参与用户库查询，写入沿用外层用户存储锁。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定账户事务。

        :param session (AsyncSession): 外层异步事务
        """
        self._session = session

    async def candidate(
        self, user_id: UUID, account_id: UUID, checkpoint_id: UUID, name: str
    ) -> AccountLocalSkill | None:
        """
        重试只复用相同账户、源目录检查点及名称的身份。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :param checkpoint_id (UUID): 完整源目录检查点
        :param name (str): 已验证名称
        :return AccountLocalSkill | None: 原始候选或空值
        """
        return await self._session.scalar(
            select(AccountLocalSkill)
            .where(
                AccountLocalSkill.user_id == user_id,
                AccountLocalSkill.account_id == account_id,
                AccountLocalSkill.source_checkpoint_id == checkpoint_id,
                AccountLocalSkill.name == name,
            )
            .execution_options(populate_existing=True)
        )

    async def candidates(
        self,
        user_id: UUID,
        account_id: UUID,
        checkpoint_id: UUID,
    ) -> Sequence[AccountLocalSkill]:
        """
        列出同一完整输入已登记的稳定候选，不从同名路径推断其他来源。

        :param user_id (UUID): 内容所有者
        :param account_id (UUID): 账户身份
        :param checkpoint_id (UUID): 原始完整输入检查点
        :return Sequence[AccountLocalSkill]: 同源候选身份
        """
        return (
            await self._session.scalars(
                select(AccountLocalSkill)
                .where(
                    AccountLocalSkill.user_id == user_id,
                    AccountLocalSkill.account_id == account_id,
                    AccountLocalSkill.source_checkpoint_id == checkpoint_id,
                )
                .order_by(AccountLocalSkill.name)
                .execution_options(populate_existing=True)
            )
        ).all()

    async def active(self, user_id: UUID, account_id: UUID) -> Sequence[AccountLocalSkill]:
        """
        只列出本账户已发布且启用的条目，候选及已移除身份不可见。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :return Sequence[AccountLocalSkill]: 按名称排序的可选本地身份
        """
        return (
            await self._session.scalars(
                select(AccountLocalSkill)
                .where(
                    AccountLocalSkill.user_id == user_id,
                    AccountLocalSkill.account_id == account_id,
                    AccountLocalSkill.status == "active",
                    AccountLocalSkill.enabled.is_(True),
                )
                .order_by(AccountLocalSkill.name)
                .execution_options(populate_existing=True)
            )
        ).all()

    async def visible(self, user_id: UUID, account_id: UUID) -> Sequence[AccountLocalSkill]:
        """
        查询账户已发布来源，保留停用条目供重新启用。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :return Sequence[AccountLocalSkill]: 当前本地来源
        """
        return (
            await self._session.scalars(
                select(AccountLocalSkill)
                .where(
                    AccountLocalSkill.user_id == user_id,
                    AccountLocalSkill.account_id == account_id,
                    AccountLocalSkill.status == "active",
                )
                .order_by(AccountLocalSkill.name)
                .execution_options(populate_existing=True)
            )
        ).all()

    async def matching(
        self, user_id: UUID, identifier: str, account_id: UUID | None
    ) -> Sequence[AccountLocalSkill]:
        """
        在明确所有者范围内解析身份或发现同名冲突。

        :param user_id (UUID): 用户身份
        :param identifier (str): 名称或稳定身份
        :param account_id (UUID | None): 可选精确账户范围
        :return Sequence[AccountLocalSkill]: 匹配的本地来源
        """
        query = select(AccountLocalSkill).where(AccountLocalSkill.user_id == user_id)
        if account_id is not None:
            query = query.where(AccountLocalSkill.account_id == account_id)
        try:
            identity = UUID(identifier)
        except ValueError:
            query = query.where(
                AccountLocalSkill.name == identifier, AccountLocalSkill.status == "active"
            )
        else:
            query = query.where(
                AccountLocalSkill.id == identity,
                AccountLocalSkill.status.in_(("active", "removed")),
            )
        return (await self._session.scalars(query.execution_options(populate_existing=True))).all()

    async def revisions(self, item: AccountLocalSkill) -> Sequence[AccountLocalSkillRevision]:
        """
        读取已授权本地来源的版本及过期墓碑。

        :param item (AccountLocalSkill): 已授权来源
        :return Sequence[AccountLocalSkillRevision]: 按编号排序的版本
        """
        return (
            await self._session.scalars(
                select(AccountLocalSkillRevision)
                .where(
                    AccountLocalSkillRevision.user_id == item.user_id,
                    AccountLocalSkillRevision.account_id == item.account_id,
                    AccountLocalSkillRevision.local_skill_id == item.id,
                )
                .order_by(AccountLocalSkillRevision.number)
            )
        ).all()

    async def revision(
        self, user_id: UUID, account_id: UUID, skill_id: UUID, revision_id: UUID
    ) -> AccountLocalSkillRevision | None:
        """
        版本必须同时属于指定用户、账户及本地来源。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :param skill_id (UUID): 本地来源身份
        :param revision_id (UUID): 不可变版本身份
        :return AccountLocalSkillRevision | None: 同源版本或空值
        """
        return await self._session.scalar(
            select(AccountLocalSkillRevision)
            .where(
                AccountLocalSkillRevision.user_id == user_id,
                AccountLocalSkillRevision.account_id == account_id,
                AccountLocalSkillRevision.local_skill_id == skill_id,
                AccountLocalSkillRevision.id == revision_id,
            )
            .execution_options(populate_existing=True)
        )

    async def branch(
        self, user_id: UUID, account_id: UUID, skill_id: UUID, revision_id: UUID
    ) -> AccountSkillState | None:
        """
        不通过名称或其他账户的相同摘要寻找运行分支。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :param skill_id (UUID): 本地来源身份
        :param revision_id (UUID): 不可变版本身份
        :return AccountSkillState | None: 精确本地分支或空值
        """
        return await self._session.scalar(
            select(AccountSkillState)
            .where(
                AccountSkillState.user_id == user_id,
                AccountSkillState.account_id == account_id,
                AccountSkillState.local_skill_id == skill_id,
                AccountSkillState.local_revision_id == revision_id,
                AccountSkillState.installation_id.is_(None),
                AccountSkillState.base_revision_id.is_(None),
                AccountSkillState.installation_epoch == 1,
            )
            .execution_options(populate_existing=True)
        )

    async def has_previous_branch(self, user_id: UUID, account_id: UUID, skill_id: UUID) -> bool:
        """
        已有状态时不得把未迁移的新本地版本隐式重置为原始内容。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :param skill_id (UUID): 本地来源身份
        :return bool: 是否存在已初始化或过期分支
        """
        return (
            await self._session.scalar(
                select(AccountSkillState.id)
                .where(
                    AccountSkillState.user_id == user_id,
                    AccountSkillState.account_id == account_id,
                    AccountSkillState.local_skill_id == skill_id,
                    AccountSkillState.head_checkpoint_id.is_not(None)
                    | AccountSkillState.expired.is_(True),
                )
                .limit(1)
            )
            is not None
        )
