"""
在单条读取语句中固定原始收尾身份及最新发布观察。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import Session
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination

StopRows = tuple[
    SessionSkillSnapshot,
    SkillSnapshotTermination | None,
    SkillFinalization | None,
    SkillPublication | None,
    Session | None,
]


class SkillStopStatusRepository:
    """
    只读当前用户的持久身份，避免多语句观察到不一致的发布阶段。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定请求数据库会话。

        :param session (AsyncSession): 异步数据库会话
        """
        self._session = session

    async def read(self, user_id: UUID, operation_id: UUID) -> StopRows | None:
        """
        删除会话后仍由不可变快照归属授权，不依赖显示会话或任务载荷。

        :param user_id (UUID): 认证用户身份
        :param operation_id (UUID): 原始快照身份
        :return StopRows | None: 同一数据库观察中的权威记录或空值
        """
        latest = (
            select(SkillPublication.id)
            .where(
                SkillPublication.finalization_id == SkillFinalization.id,
                SkillPublication.user_id == user_id,
            )
            .order_by(SkillPublication.attempt.desc())
            .limit(1)
            .correlate(SkillFinalization)
            .scalar_subquery()
        )
        row = (
            await self._session.execute(
                select(
                    SessionSkillSnapshot,
                    SkillSnapshotTermination,
                    SkillFinalization,
                    SkillPublication,
                    Session,
                )
                .select_from(SessionSkillSnapshot)
                .outerjoin(
                    SkillSnapshotTermination,
                    SkillSnapshotTermination.snapshot_id == SessionSkillSnapshot.id,
                )
                .outerjoin(
                    SkillFinalization, SkillFinalization.snapshot_id == SessionSkillSnapshot.id
                )
                .outerjoin(SkillPublication, SkillPublication.id == latest)
                .outerjoin(Session, Session.id == SessionSkillSnapshot.session_id)
                .where(
                    SessionSkillSnapshot.user_id == user_id, SessionSkillSnapshot.id == operation_id
                )
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        return tuple(row) if row is not None else None
