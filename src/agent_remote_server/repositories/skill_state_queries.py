"""
按所有者、账户和稳定来源查询检查点历史及尚待完整上传的收尾。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_snapshots import (
    SessionSkillSnapshot,
    SessionSkillSnapshotItem,
    SkillFinalization,
)
from agent_remote_server.models.skill_state import (
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)


class SkillStateQueryRepository:
    """
    只读仓储不建立状态或内容引用，范围由已授权来源决定。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        使用请求事务读取一致视图。

        :param session (AsyncSession): 异步数据库事务
        """
        self._session = session

    async def checkpoint(self, user_id: UUID, checkpoint_id: UUID) -> SkillCheckpoint | None:
        """
        检查点身份本身不能绕过所有者约束。

        :param user_id (UUID): 当前用户
        :param checkpoint_id (UUID): 对象身份
        :return SkillCheckpoint | None: 同用户对象或空值
        """
        return await self._session.scalar(
            select(SkillCheckpoint)
            .where(SkillCheckpoint.user_id == user_id, SkillCheckpoint.id == checkpoint_id)
            .execution_options(populate_existing=True)
        )

    async def branch(self, checkpoint: SkillCheckpoint) -> AccountSkillState | None:
        """
        从授权检查点追溯固定分支，不按当前名称推断来源。

        :param checkpoint (SkillCheckpoint): 已授权检查点
        :return AccountSkillState | None: 单项固定分支或目录范围空值
        """
        if checkpoint.state_id is None:
            return None
        return await self._session.scalar(
            select(AccountSkillState)
            .where(
                AccountSkillState.user_id == checkpoint.user_id,
                AccountSkillState.account_id == checkpoint.account_id,
                AccountSkillState.id == checkpoint.state_id,
            )
            .execution_options(populate_existing=True)
        )

    async def local_source(
        self, user_id: UUID, account_id: UUID, identifier: str
    ) -> AccountLocalSkill | None:
        """
        名称只定位当前活动来源，稳定身份也允许读取已移除来源历史。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 已授权账户
        :param identifier (str): 名称或稳定身份
        :return AccountLocalSkill | None: 同账户来源或空值
        """
        query = select(AccountLocalSkill).where(
            AccountLocalSkill.user_id == user_id, AccountLocalSkill.account_id == account_id
        )
        try:
            identity = UUID(identifier)
        except ValueError:
            query = query.where(
                AccountLocalSkill.name == identifier, AccountLocalSkill.status == "active"
            )
        else:
            query = query.where(AccountLocalSkill.id == identity)
        return await self._session.scalar(query.execution_options(populate_existing=True))

    async def checkpoints(
        self,
        user_id: UUID,
        account_id: UUID,
        skill_id: UUID | None,
        limit: int,
        before: SkillCheckpoint | None,
    ) -> Sequence[SkillCheckpoint]:
        """
        单项列出所有版本和安装纪元，目录范围只列出完整目录对象。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 已授权账户
        :param skill_id (UUID | None): 稳定来源身份，空值表示完整目录
        :param limit (int): 最大返回数量
        :param before (SkillCheckpoint | None): 已验证同范围游标
        :return Sequence[SkillCheckpoint]: 按时间与身份倒序的有界历史
        """
        query = select(SkillCheckpoint).where(
            SkillCheckpoint.user_id == user_id, SkillCheckpoint.account_id == account_id
        )
        if skill_id is None:
            query = query.where(SkillCheckpoint.scope == "directory")
        else:
            query = query.join(
                AccountSkillState, AccountSkillState.id == SkillCheckpoint.state_id
            ).where(
                or_(
                    AccountSkillState.installation_id == skill_id,
                    AccountSkillState.local_skill_id == skill_id,
                )
            )
        if before is not None:
            query = query.where(
                or_(
                    SkillCheckpoint.created_at < before.created_at,
                    and_(
                        SkillCheckpoint.created_at == before.created_at,
                        SkillCheckpoint.id < before.id,
                    ),
                )
            )
        return (
            await self._session.scalars(
                query.order_by(SkillCheckpoint.created_at.desc(), SkillCheckpoint.id.desc()).limit(
                    limit
                )
            )
        ).all()

    async def members(
        self, checkpoint: SkillCheckpoint, limit: int, cursor: str | None
    ) -> list[tuple[SkillDirectoryMember, AccountSkillState, SkillCheckpoint]]:
        """
        成员引用与同账户精确分支一起读取，避免按名称猜测历史来源。

        :param checkpoint (SkillCheckpoint): 已授权完整目录
        :param limit (int): 页大小
        :param cursor (str | None): 上页末尾成员名称
        :return list[tuple[SkillDirectoryMember, AccountSkillState, SkillCheckpoint]]: 原成员
        """
        query = (
            select(SkillDirectoryMember, AccountSkillState, SkillCheckpoint)
            .join(AccountSkillState, AccountSkillState.id == SkillDirectoryMember.state_id)
            .join(SkillCheckpoint, SkillCheckpoint.id == SkillDirectoryMember.checkpoint_id)
            .where(
                SkillDirectoryMember.user_id == checkpoint.user_id,
                SkillDirectoryMember.account_id == checkpoint.account_id,
                SkillDirectoryMember.directory_checkpoint_id == checkpoint.id,
            )
        )
        if cursor is not None:
            query = query.where(SkillDirectoryMember.entry_name > cursor)
        return [
            (row[0], row[1], row[2])
            for row in (
                await self._session.execute(
                    query.order_by(SkillDirectoryMember.entry_name).limit(limit)
                )
            ).all()
        ]

    async def source_finalization(self, checkpoint: SkillCheckpoint) -> SkillFinalization | None:
        """
        读取产生检查点的会话收尾，状态仅表示该会话结果而非当前 head。

        :param checkpoint (SkillCheckpoint): 已授权检查点
        :return SkillFinalization | None: 来源会话收尾或空值
        """
        if checkpoint.source_session_reference_id is None:
            return None
        return await self._session.scalar(
            select(SkillFinalization)
            .join(SessionSkillSnapshot, SessionSkillSnapshot.id == SkillFinalization.snapshot_id)
            .where(
                SkillFinalization.user_id == checkpoint.user_id,
                SkillFinalization.account_id == checkpoint.account_id,
                SessionSkillSnapshot.session_reference_id == checkpoint.source_session_reference_id,
            )
        )

    async def pending(
        self,
        user_id: UUID,
        account_id: UUID,
        skill_id: UUID | None,
        limit: int,
        cursor: UUID | None,
    ) -> list[tuple[SkillFinalization, SessionSkillSnapshot]]:
        """
        未完成上传没有检查点，使用独立身份分页且严格限制原快照来源。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 已授权账户
        :param skill_id (UUID | None): 稳定来源或完整目录范围
        :param limit (int): 最大读取数量
        :param cursor (UUID | None): 上页收尾身份，按稳定身份排序
        :return list[tuple[SkillFinalization, SessionSkillSnapshot]]: 收尾及原始快照
        """
        query = self._pending_scope(user_id, account_id, skill_id).where(
            SkillFinalization.status == "upload_pending"
        )
        if cursor is not None:
            query = query.where(SkillFinalization.id > cursor)
        return [
            (row[0], row[1])
            for row in (
                await self._session.execute(query.order_by(SkillFinalization.id).limit(limit))
            ).all()
        ]

    async def pending_cursor_valid(
        self, user_id: UUID, account_id: UUID, skill_id: UUID | None, cursor: UUID
    ) -> bool:
        """
        已完成上传仍可用原游标翻页，但不能替用其他账户或来源的身份。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 指定账户
        :param skill_id (UUID | None): 单项来源或目录范围
        :param cursor (UUID): 收尾身份
        :return bool: 是否属于同一授权范围
        """
        query = self._pending_scope(user_id, account_id, skill_id).where(
            SkillFinalization.id == cursor
        )
        return await self._session.scalar(query) is not None

    def _pending_scope(
        self, user_id: UUID, account_id: UUID, skill_id: UUID | None
    ) -> Select[tuple[SkillFinalization, SessionSkillSnapshot]]:
        """
        待上传页和游标校验共享原快照来源约束。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 指定账户
        :param skill_id (UUID | None): 单项来源或目录范围
        :return Select[tuple[SkillFinalization, SessionSkillSnapshot]]: 同范围查询
        """
        query = (
            select(SkillFinalization, SessionSkillSnapshot)
            .join(SessionSkillSnapshot, SessionSkillSnapshot.id == SkillFinalization.snapshot_id)
            .where(
                SkillFinalization.user_id == user_id,
                SkillFinalization.account_id == account_id,
            )
        )
        if skill_id is not None:
            membership = (
                select(SessionSkillSnapshotItem.snapshot_id)
                .join(AccountSkillState, AccountSkillState.id == SessionSkillSnapshotItem.state_id)
                .where(
                    SessionSkillSnapshotItem.snapshot_id == SkillFinalization.snapshot_id,
                    or_(
                        AccountSkillState.installation_id == skill_id,
                        AccountSkillState.local_skill_id == skill_id,
                    ),
                )
                .exists()
            )
            query = query.where(membership)
        return query
