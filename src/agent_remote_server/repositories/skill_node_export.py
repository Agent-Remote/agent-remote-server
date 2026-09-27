"""
读取冻结导出需要的原始快照和实时身份，不以当前账户绑定替换历史来源。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import AuthToken, Node, SshKey, User, UserDevice
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination

ExportRows = tuple[SessionSkillSnapshot, SkillSnapshotTermination | None, SkillFinalization | None]


class NodeExportRepository:
    """
    每次重验刷新已有 ORM 实体，避免在长事务中沿用旧撤销观察。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定调用方请求事务。

        :param session (AsyncSession): 当前数据库会话
        """
        self.session = session

    async def identity(
        self, user_id: UUID, token_id: UUID, device_id: UUID, key_id: UUID, now: datetime
    ) -> AuthToken | None:
        """
        在同一查询中要求原用户令牌、用户、设备和精确公钥全部活跃。

        :param user_id (UUID): 原内容所有者
        :param token_id (UUID): 签发时的用户令牌身份
        :param device_id (UUID): SSH 强制命令设备
        :param key_id (UUID): SSH 强制命令公钥
        :param now (datetime): 当前 UTC 时刻
        :return AuthToken | None: 尚有效的原用户令牌或空值
        """
        return await self.session.scalar(
            select(AuthToken)
            .join(User, User.id == AuthToken.user_id)
            .join(UserDevice, UserDevice.user_id == User.id)
            .join(SshKey, SshKey.user_device_id == UserDevice.id)
            .where(
                User.id == user_id,
                User.status == "active",
                AuthToken.id == token_id,
                AuthToken.token_type == "user",
                AuthToken.status == "active",
                AuthToken.revoked_at.is_(None),
                AuthToken.expires_at > now,
                UserDevice.id == device_id,
                UserDevice.status == "active",
                SshKey.id == key_id,
                SshKey.status == "active",
                SshKey.revoked_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )

    async def snapshot(self, user_id: UUID, snapshot_id: UUID) -> ExportRows | None:
        """
        读取同用户的永久原快照和可选收尾观察，不依赖尚未上传的 Server 内容。

        :param user_id (UUID): 认证所有者
        :param snapshot_id (UUID): 原精确快照
        :return ExportRows | None: 一致读取的原始身份及已知收尾事实
        """
        row = (
            await self.session.execute(
                select(SessionSkillSnapshot, SkillSnapshotTermination, SkillFinalization)
                .outerjoin(
                    SkillSnapshotTermination,
                    SkillSnapshotTermination.snapshot_id == SessionSkillSnapshot.id,
                )
                .outerjoin(
                    SkillFinalization, SkillFinalization.snapshot_id == SessionSkillSnapshot.id
                )
                .where(
                    SessionSkillSnapshot.user_id == user_id,
                    SessionSkillSnapshot.id == snapshot_id,
                )
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        return tuple(row) if row is not None else None

    async def node(self, node_id: UUID) -> Node | None:
        """
        重新读取唯一来源节点的连接与管理状态。

        :param node_id (UUID): 原快照中的节点
        :return Node | None: 当前节点记录或空值
        """
        return await self.session.get(Node, node_id, populate_existing=True)
