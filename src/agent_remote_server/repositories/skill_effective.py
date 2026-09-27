"""
所有有效集合查询 SQL 保留所有者、账户和原始来源约束。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_publications import SkillPublication, SkillPublicationBranch
from agent_remote_server.models.skill_snapshots import (
    SessionSkillSnapshot,
    SessionSkillSnapshotItem,
    SkillFinalization,
)
from agent_remote_server.models.skill_state import AccountSkillState, SkillCheckpoint


class SkillEffectiveRepository:
    """
    查询不锁定任意节点任务，也不创建运行分支。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        共享外层用户读事务。

        :param session (AsyncSession): 当前事务
        """
        self.session = session

    async def members(
        self, snapshot: SessionSkillSnapshot, limit: int, cursor: str | None
    ) -> list[tuple[SessionSkillSnapshotItem, AccountSkillState, SkillCheckpoint]]:
        """
        固定成员按保存名称分页，关联只用于不可变分支来源与当前内容保留标记。

        :param snapshot (SessionSkillSnapshot): 已授权原快照
        :param limit (int): 有界数量
        :param cursor (str | None): 上页末尾名称
        :return list[tuple[SessionSkillSnapshotItem, AccountSkillState, SkillCheckpoint]]: 原成员
        """
        name = SessionSkillSnapshotItem.entry_name.collate(
            "C" if self.session.get_bind().dialect.name == "postgresql" else "BINARY"
        )
        query = (
            select(SessionSkillSnapshotItem, AccountSkillState, SkillCheckpoint)
            .join(AccountSkillState, AccountSkillState.id == SessionSkillSnapshotItem.state_id)
            .join(SkillCheckpoint, SkillCheckpoint.id == SessionSkillSnapshotItem.checkpoint_id)
            .where(
                SessionSkillSnapshotItem.user_id == snapshot.user_id,
                SessionSkillSnapshotItem.account_id == snapshot.account_id,
                SessionSkillSnapshotItem.snapshot_id == snapshot.id,
            )
        )
        if cursor is not None:
            query = query.where(name > cursor)
        return [
            (row[0], row[1], row[2])
            for row in (await self.session.execute(query.order_by(name).limit(limit))).all()
        ]

    async def cursor_exists(self, snapshot: SessionSkillSnapshot, cursor: str) -> bool:
        """
        名称游标必须来自该已授权快照，不能跳过伪造位置。

        :param snapshot (SessionSkillSnapshot): 原快照
        :param cursor (str): 上页末尾名称
        :return bool: 是否为原成员
        """
        return (
            await self.session.scalar(
                select(SessionSkillSnapshotItem.state_id).where(
                    SessionSkillSnapshotItem.user_id == snapshot.user_id,
                    SessionSkillSnapshotItem.snapshot_id == snapshot.id,
                    SessionSkillSnapshotItem.entry_name == cursor,
                )
            )
            is not None
        )

    async def conflicts(
        self, user_id: UUID, account_id: UUID, source_id: UUID
    ) -> tuple[int, UUID | None, int, UUID | None]:
        """
        返回两个独立冲突域的完整计数和最近查询身份，不把旧纪元冲突冒充启动阻断。

        :param user_id (UUID): 用户
        :param account_id (UUID): 账户
        :param source_id (UUID): 库或本地来源
        :return tuple[int, UUID | None, int, UUID | None]: 发布数量与最新身份以及迁移数量与最新身份
        """
        branches = select(AccountSkillState.id).where(
            AccountSkillState.user_id == user_id,
            AccountSkillState.account_id == account_id,
            (AccountSkillState.installation_id == source_id)
            | (AccountSkillState.local_skill_id == source_id),
        )
        publications = select(SkillPublication.id, SkillPublication.created_at).where(
            SkillPublication.user_id == user_id,
            SkillPublication.account_id == account_id,
            SkillPublication.status == "conflicted",
            SkillPublication.id.in_(
                select(SkillPublicationBranch.publication_id).where(
                    SkillPublicationBranch.user_id == user_id,
                    SkillPublicationBranch.state_id.in_(branches),
                )
            ),
        )
        migrations = select(SkillBranchPreparation.id, SkillBranchPreparation.created_at).where(
            SkillBranchPreparation.user_id == user_id,
            SkillBranchPreparation.account_id == account_id,
            SkillBranchPreparation.installation_id == source_id,
            SkillBranchPreparation.status == "conflicted",
        )
        publication_count = int(
            await self.session.scalar(select(func.count()).select_from(publications.subquery()))
            or 0
        )
        migration_count = int(
            await self.session.scalar(select(func.count()).select_from(migrations.subquery())) or 0
        )
        latest_publication = await self.session.scalar(
            publications.order_by(
                SkillPublication.created_at.desc(), SkillPublication.id.desc()
            ).limit(1)
        )
        latest_migration = await self.session.scalar(
            migrations.order_by(
                SkillBranchPreparation.created_at.desc(), SkillBranchPreparation.id.desc()
            ).limit(1)
        )
        return publication_count, latest_publication, migration_count, latest_migration

    async def sync_times(
        self, user_id: UUID, account_id: UUID, source_id: UUID
    ) -> tuple[datetime | None, bool]:
        """
        同步只取专用完成时间，旧已持久化记录保留未知标记。

        :param user_id (UUID): 用户
        :param account_id (UUID): 账户
        :param source_id (UUID): 来源身份
        :return tuple[datetime | None, bool]: 最近已记录同步及未知历史标记
        """
        snapshots = (
            select(SessionSkillSnapshotItem.snapshot_id)
            .join(AccountSkillState, AccountSkillState.id == SessionSkillSnapshotItem.state_id)
            .where(
                SessionSkillSnapshotItem.user_id == user_id,
                SessionSkillSnapshotItem.account_id == account_id,
                (AccountSkillState.installation_id == source_id)
                | (AccountSkillState.local_skill_id == source_id),
            )
        )
        row = (
            await self.session.execute(
                select(
                    func.max(SkillFinalization.persisted_at),
                    func.sum(case((SkillFinalization.persisted_at.is_(None), 1), else_=0)),
                ).where(
                    SkillFinalization.user_id == user_id,
                    SkillFinalization.account_id == account_id,
                    SkillFinalization.snapshot_id.in_(snapshots),
                    SkillFinalization.status != "upload_pending",
                )
            )
        ).one()
        return row[0], bool(row[1])
