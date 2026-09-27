"""
查询精确节点快照、不可变收尾和当前上传尝试。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.db import Base
from agent_remote_server.models import User
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination
from agent_remote_server.models.skill_transfers import SkillFinalizationTransfer


class SkillFinalizationRepository:
    """
    写入与租约替换由外层用户存储锁串行化。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定请求事务，不自行提交。

        :param session (AsyncSession): 异步请求事务
        """
        self._session = session

    async def snapshot(self, node_id: UUID, snapshot_id: UUID) -> SessionSkillSnapshot | None:
        """
        仅返回当前活跃用户交给该节点的精确快照。

        :param node_id (UUID): 认证节点
        :param snapshot_id (UUID): 快照身份
        :return SessionSkillSnapshot | None: 已授权快照
        """
        return await self._session.scalar(
            select(SessionSkillSnapshot)
            .join(User, User.id == SessionSkillSnapshot.user_id)
            .where(
                SessionSkillSnapshot.node_id == node_id,
                SessionSkillSnapshot.id == snapshot_id,
                User.status == "active",
            )
            .execution_options(populate_existing=True)
        )

    async def receipt(self, node_id: UUID, finalization_id: UUID) -> SkillFinalization | None:
        """
        收尾 ID 不能授权其他节点读取或修改输入。

        :param node_id (UUID): 认证节点
        :param finalization_id (UUID): 收尾身份
        :return SkillFinalization | None: 已授权收尾
        """
        return await self._session.scalar(
            select(SkillFinalization)
            .join(User, User.id == SkillFinalization.user_id)
            .where(
                SkillFinalization.node_id == node_id,
                SkillFinalization.id == finalization_id,
                User.status == "active",
            )
            .execution_options(populate_existing=True)
        )

    async def by_key(self, user_id: UUID, key: str) -> SkillFinalization | None:
        """
        在用户范围中检查幂等键，不允许跨快照复用原始请求。

        :param user_id (UUID): 已授权用户
        :param key (str): 原始幂等键
        :return SkillFinalization | None: 此键已登记的收尾
        """
        return await self._session.scalar(
            select(SkillFinalization)
            .where(SkillFinalization.user_id == user_id, SkillFinalization.idempotency_key == key)
            .execution_options(populate_existing=True)
        )

    async def termination(self, snapshot_id: UUID) -> SkillSnapshotTermination | None:
        """
        在原始快照授权与用户锁之后读取不可变终止输入。

        :param snapshot_id (UUID): 已授权快照身份
        :return SkillSnapshotTermination | None: 原始观察或未上报
        """
        return await self._session.scalar(
            select(SkillSnapshotTermination)
            .where(SkillSnapshotTermination.snapshot_id == snapshot_id)
            .execution_options(populate_existing=True)
        )

    async def transfer(self, receipt: SkillFinalization) -> SkillFinalizationTransfer | None:
        """
        取得该输入当前的唯一上传绑定。

        :param receipt (SkillFinalization): 已授权收尾
        :return SkillFinalizationTransfer | None: 当前传输或旧版缺失绑定
        """
        return await self._session.scalar(
            select(SkillFinalizationTransfer)
            .where(
                SkillFinalizationTransfer.user_id == receipt.user_id,
                SkillFinalizationTransfer.finalization_id == receipt.id,
                SkillFinalizationTransfer.incoming_digest == receipt.incoming_digest,
            )
            .execution_options(populate_existing=True)
        )

    def add(self, row: Base) -> None:
        """
        登记同一事务中的实体。

        :param row (Base): 已验证实体
        """
        self._session.add(row)

    async def flush(self) -> None:
        """
        验证约束但保留外层原子提交边界。
        """
        await self._session.flush()
