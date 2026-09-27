"""
按用户与账户查询冲突计划、原始输入和幂等操作。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from agent_remote_server.models.skill_publications import SkillPublication, SkillPublicationBranch
from agent_remote_server.models.skill_resolution import (
    SkillResolutionChoice,
    SkillResolutionOperation,
    SkillResolutionPlan,
)
from agent_remote_server.models.skill_state import AccountSkillState


class SkillResolutionRepository:
    """
    所有计划写入在外层用户锁和单一原子事务中完成。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        共享外层请求事务。

        :param session (AsyncSession): 请求数据库会话
        """
        self._session = session

    async def publication(self, user_id: UUID, publication_id: UUID) -> SkillPublication | None:
        """
        发布 ID 不能跨用户授权查询私有冲突。

        :param user_id (UUID): 当前用户
        :param publication_id (UUID): 冲突尝试身份
        :return SkillPublication | None: 本用户尝试或空值
        """
        return await self._session.scalar(
            select(SkillPublication)
            .where(
                SkillPublication.user_id == user_id,
                SkillPublication.id == publication_id,
            )
            .execution_options(populate_existing=True)
        )

    async def publications(
        self,
        user_id: UUID,
        account_id: UUID,
        limit: int,
        before: SkillPublication | None,
        skill_id: UUID | None = None,
    ) -> Sequence[SkillPublication]:
        """
        按稳定时间和身份倒序读取有界冲突页。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 已授权账户
        :param limit (int): 最大读取条数
        :param before (SkillPublication | None): 同账户已授权游标
        :param skill_id (UUID | None): 可选稳定库或本地来源身份
        :return Sequence[SkillPublication]: 有界冲突历史
        """
        query = select(SkillPublication).where(
            SkillPublication.user_id == user_id,
            SkillPublication.account_id == account_id,
            SkillPublication.status.in_(("conflicted", "superseded")),
        )
        if skill_id is not None:
            query = query.where(_source_membership(skill_id))
        if before is not None:
            query = query.where(
                or_(
                    SkillPublication.created_at < before.created_at,
                    and_(
                        SkillPublication.created_at == before.created_at,
                        SkillPublication.id < before.id,
                    ),
                )
            )
        return (
            await self._session.scalars(
                query.order_by(
                    SkillPublication.created_at.desc(),
                    SkillPublication.id.desc(),
                ).limit(limit)
            )
        ).all()

    async def has_source(self, publication: SkillPublication, skill_id: UUID) -> bool:
        """
        游标使用与列表相同的保存分支范围，不能借用其他来源的时间边界。

        :param publication (SkillPublication): 已授权游标尝试
        :param skill_id (UUID): 已解析稳定来源
        :return bool: 原尝试是否包含该来源
        """
        return (
            await self._session.scalar(
                select(SkillPublication.id).where(
                    SkillPublication.id == publication.id,
                    SkillPublication.user_id == publication.user_id,
                    SkillPublication.account_id == publication.account_id,
                    _source_membership(skill_id),
                )
            )
            is not None
        )

    async def branches(self, publication: SkillPublication) -> Sequence[SkillPublicationBranch]:
        """
        返回冲突时保存的精确分支前置条件。

        :param publication (SkillPublication): 已授权尝试
        :return Sequence[SkillPublicationBranch]: 已保存的比较条件
        """
        return (
            await self._session.scalars(
                select(SkillPublicationBranch)
                .where(
                    SkillPublicationBranch.user_id == publication.user_id,
                    SkillPublicationBranch.account_id == publication.account_id,
                    SkillPublicationBranch.publication_id == publication.id,
                )
                .order_by(SkillPublicationBranch.entry_name)
            )
        ).all()

    async def plan(self, publication: SkillPublication) -> SkillResolutionPlan | None:
        """
        不在只读或预览时自动创建计划。

        :param publication (SkillPublication): 已授权尝试
        :return SkillResolutionPlan | None: 已保存计划或空值
        """
        return await self._session.scalar(
            select(SkillResolutionPlan)
            .where(
                SkillResolutionPlan.user_id == publication.user_id,
                SkillResolutionPlan.account_id == publication.account_id,
                SkillResolutionPlan.publication_id == publication.id,
            )
            .execution_options(populate_existing=True)
        )

    async def choices(self, publication: SkillPublication) -> Sequence[SkillResolutionChoice]:
        """
        只加载该尝试的人工内容及选择范围。

        :param publication (SkillPublication): 已授权尝试
        :return Sequence[SkillResolutionChoice]: 已保存选择
        """
        return (
            await self._session.scalars(
                select(SkillResolutionChoice)
                .where(
                    SkillResolutionChoice.user_id == publication.user_id,
                    SkillResolutionChoice.account_id == publication.account_id,
                    SkillResolutionChoice.publication_id == publication.id,
                )
                .order_by(SkillResolutionChoice.selector_key)
                .execution_options(populate_existing=True)
            )
        ).all()

    async def operation_by_id(
        self, user_id: UUID, operation_id: UUID
    ) -> SkillResolutionOperation | None:
        """
        按原受理身份和认证用户查询，不执行任何解决选择。

        :param user_id (UUID): 当前认证所有者
        :param operation_id (UUID): 原始受理身份
        :return SkillResolutionOperation | None: 原始回执或不存在
        """
        return await self._session.scalar(
            select(SkillResolutionOperation).where(
                SkillResolutionOperation.user_id == user_id,
                SkillResolutionOperation.id == operation_id,
            )
        )

    async def operation(self, user_id: UUID, key: str) -> SkillResolutionOperation | None:
        """
        幂等键绑定原始用户命令，不能在另一冲突上重复使用。

        :param user_id (UUID): 当前用户
        :param key (str): 原始客户端键
        :return SkillResolutionOperation | None: 已受理操作或空值
        """
        return await self._session.scalar(
            select(SkillResolutionOperation).where(
                SkillResolutionOperation.user_id == user_id,
                SkillResolutionOperation.idempotency_key == key,
            )
        )

    async def remove_choice(self, choice: SkillResolutionChoice) -> None:
        """
        新范围覆盖旧选择时只释放该计划引用，不删除对象或原始冲突。

        :param choice (SkillResolutionChoice): 已授权旧选择
        """
        await self._session.delete(choice)


def _source_membership(skill_id: UUID) -> ColumnElement[bool]:
    """
    完整目录冲突关联全部观察分支，不仅限于发生改动的成员。

    :param skill_id (UUID): 精确稳定来源身份
    :return ColumnElement[bool]: 与外层发布尝试关联的存在性条件
    """
    return (
        select(SkillPublicationBranch.publication_id)
        .join(
            AccountSkillState,
            and_(
                AccountSkillState.id == SkillPublicationBranch.state_id,
                AccountSkillState.user_id == SkillPublicationBranch.user_id,
                AccountSkillState.account_id == SkillPublicationBranch.account_id,
            ),
        )
        .where(
            SkillPublicationBranch.publication_id == SkillPublication.id,
            SkillPublicationBranch.user_id == SkillPublication.user_id,
            SkillPublicationBranch.account_id == SkillPublication.account_id,
            or_(
                AccountSkillState.installation_id == skill_id,
                AccountSkillState.local_skill_id == skill_id,
            ),
        )
        .exists()
    )
