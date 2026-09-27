"""
封装账户运行分支、精确快照和节点准备绑定的归属查询。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.db import Base
from agent_remote_server.models import NodeTask, Session, User
from agent_remote_server.models.skill_snapshots import (
    SessionSkillSnapshot,
    SessionSkillSnapshotItem,
    SkillFinalization,
)
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)


class SkillRuntimeRepository:
    """
    所有写入调用方必须先取得用户存储锁，查询不能扩展授权范围。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定外层原子事务。

        :param session (AsyncSession): 异步数据库事务
        """
        self._session = session

    async def directory(self, user_id: UUID, account_id: UUID) -> AccountSkillDirectoryState | None:
        """
        取得本用户账户目录权威。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :return AccountSkillDirectoryState | None: 已授权目录
        """
        return await self._session.scalar(
            select(AccountSkillDirectoryState)
            .where(
                AccountSkillDirectoryState.user_id == user_id,
                AccountSkillDirectoryState.account_id == account_id,
            )
            .execution_options(populate_existing=True)
        )

    async def branch(
        self,
        user_id: UUID,
        account_id: UUID,
        installation_id: UUID,
        installation_epoch: int,
        revision_id: UUID,
    ) -> AccountSkillState | None:
        """
        精确查找账户、安装纪元与版本的独立分支。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :param installation_id (UUID): 安装身份
        :param installation_epoch (int): 当前安装纪元
        :param revision_id (UUID): 有效原始版本
        :return AccountSkillState | None: 已授权分支
        """
        return await self._session.scalar(
            select(AccountSkillState)
            .where(
                AccountSkillState.user_id == user_id,
                AccountSkillState.account_id == account_id,
                AccountSkillState.installation_id == installation_id,
                AccountSkillState.installation_epoch == installation_epoch,
                AccountSkillState.base_revision_id == revision_id,
            )
            .execution_options(populate_existing=True)
        )

    async def checkpoint(
        self, user_id: UUID, account_id: UUID, checkpoint_id: UUID
    ) -> SkillCheckpoint | None:
        """
        只返回同用户同账户的完整树视图。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :param checkpoint_id (UUID): 检查点身份
        :return SkillCheckpoint | None: 已授权检查点
        """
        return await self._session.scalar(
            select(SkillCheckpoint)
            .where(
                SkillCheckpoint.user_id == user_id,
                SkillCheckpoint.account_id == account_id,
                SkillCheckpoint.id == checkpoint_id,
            )
            .execution_options(populate_existing=True)
        )

    async def has_previous_branch(
        self, user_id: UUID, account_id: UUID, installation_id: UUID, installation_epoch: int
    ) -> bool:
        """
        判断当前安装纪元是否已有运行分支，阻止未迁移时静默切回原始包。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :param installation_id (UUID): 安装身份
        :param installation_epoch (int): 当前安装纪元
        :return bool: 是否存在已初始化或过期的运行分支
        """
        return (
            await self._session.scalar(
                select(AccountSkillState.id)
                .where(
                    AccountSkillState.user_id == user_id,
                    AccountSkillState.account_id == account_id,
                    AccountSkillState.installation_id == installation_id,
                    AccountSkillState.installation_epoch == installation_epoch,
                    (
                        AccountSkillState.head_checkpoint_id.is_not(None)
                        | AccountSkillState.expired.is_(True)
                    ),
                )
                .limit(1)
            )
            is not None
        )

    async def members(self, checkpoint: SkillCheckpoint) -> Sequence[SkillDirectoryMember]:
        """
        列出已授权目录全部成员，停用项仍保留身份。

        :param checkpoint (SkillCheckpoint): 已授权目录检查点
        :return Sequence[SkillDirectoryMember]: 不可变成员表
        """
        return (
            await self._session.scalars(
                select(SkillDirectoryMember).where(
                    SkillDirectoryMember.user_id == checkpoint.user_id,
                    SkillDirectoryMember.account_id == checkpoint.account_id,
                    SkillDirectoryMember.directory_checkpoint_id == checkpoint.id,
                )
            )
        ).all()

    async def session(self, user_id: UUID, session_id: UUID) -> Session | None:
        """
        在用户锁之后锁定会话，固定节点和账户绑定。

        :param user_id (UUID): 用户身份
        :param session_id (UUID): 会话身份
        :return Session | None: 本用户会话
        """
        return await self._session.scalar(
            select(Session)
            .where(Session.user_id == user_id, Session.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def task(self, node_id: UUID, task_id: UUID) -> NodeTask | None:
        """
        取得绑定节点的精确准备任务。

        :param node_id (UUID): 节点身份
        :param task_id (UUID): 数据库任务身份
        :return NodeTask | None: 同节点任务
        """
        return await self._session.scalar(
            select(NodeTask)
            .where(NodeTask.node_id == node_id, NodeTask.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def snapshot_for_session(
        self, user_id: UUID, session_id: UUID
    ) -> SessionSkillSnapshot | None:
        """
        重试始终查询第一次固定的快照，不重新选择规则。

        :param user_id (UUID): 用户身份
        :param session_id (UUID): 原始会话身份
        :return SessionSkillSnapshot | None: 已有精确快照
        """
        return await self._session.scalar(
            select(SessionSkillSnapshot)
            .where(
                SessionSkillSnapshot.user_id == user_id,
                SessionSkillSnapshot.session_reference_id == session_id,
            )
            .execution_options(populate_existing=True)
        )

    async def snapshot_items(
        self, snapshot: SessionSkillSnapshot
    ) -> Sequence[SessionSkillSnapshotItem]:
        """
        读取已授权快照实际暴露的成员。

        :param snapshot (SessionSkillSnapshot): 已授权快照
        :return Sequence[SessionSkillSnapshotItem]: 不可变物化成员
        """
        return (
            await self._session.scalars(
                select(SessionSkillSnapshotItem)
                .where(
                    SessionSkillSnapshotItem.user_id == snapshot.user_id,
                    SessionSkillSnapshotItem.account_id == snapshot.account_id,
                    SessionSkillSnapshotItem.snapshot_id == snapshot.id,
                )
                .order_by(SessionSkillSnapshotItem.entry_name)
            )
        ).all()

    async def node_snapshot(
        self, node_id: UUID, snapshot_id: UUID, task_id: UUID
    ) -> SessionSkillSnapshot | None:
        """
        节点仅能定位自有任务的快照，禁用用户不授予内容权限。

        :param node_id (UUID): 已认证节点
        :param snapshot_id (UUID): 精确快照身份
        :param task_id (UUID): 精确准备任务身份
        :return SessionSkillSnapshot | None: 匹配归属的快照
        """
        return await self._session.scalar(
            select(SessionSkillSnapshot)
            .join(User, User.id == SessionSkillSnapshot.user_id)
            .where(
                SessionSkillSnapshot.node_id == node_id,
                SessionSkillSnapshot.id == snapshot_id,
                SessionSkillSnapshot.prepare_task_id == task_id,
                User.status == "active",
            )
            .execution_options(populate_existing=True)
        )

    async def finalization(self, snapshot: SessionSkillSnapshot) -> SkillFinalization | None:
        """
        返回已授权快照的唯一收尾提交。

        :param snapshot (SessionSkillSnapshot): 已授权快照
        :return SkillFinalization | None: 完整收尾状态或尚未提交
        """
        return await self._session.scalar(
            select(SkillFinalization)
            .where(
                SkillFinalization.user_id == snapshot.user_id,
                SkillFinalization.account_id == snapshot.account_id,
                SkillFinalization.node_id == snapshot.node_id,
                SkillFinalization.snapshot_id == snapshot.id,
            )
            .execution_options(populate_existing=True)
        )

    async def has_task_snapshot(self, node_id: UUID, task_id: UUID) -> bool:
        """
        从持久化引用识别受管任务，防止删除载荷标记后绕回旧结果路径。

        :param node_id (UUID): 已认证节点
        :param task_id (UUID): 精确任务记录
        :return bool: 是否存在绑定快照
        """
        return (
            await self._session.scalar(
                select(SessionSkillSnapshot.id)
                .where(
                    SessionSkillSnapshot.node_id == node_id,
                    SessionSkillSnapshot.prepare_task_id == task_id,
                )
                .limit(1)
            )
            is not None
        )

    def add(self, row: Base) -> None:
        """
        登记同一事务中的已验证实体。

        :param row (Base): 已验证实体
        """
        self._session.add(row)

    async def flush(self) -> None:
        """
        验证约束但不提交外层事务。
        """
        await self._session.flush()
