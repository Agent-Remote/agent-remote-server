"""
解释原始收尾操作的进程确认、内容保存与最新发布状态。
"""

from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_stop_status import SkillStopStatusRepository
from agent_remote_server.schemas.skill_stop_status import SaveStatus, SkillStopStatus
from agent_remote_server.schemas.skill_terminations import CaptureError
from agent_remote_server.services.skills.content import SkillContentError


class SkillStopStatusService:
    """
    不启动上传、不修复元数据，也不把停止任务成功推断为已保存。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        构造只读仓储。

        :param session (AsyncSession): 请求数据库会话
        """
        self.repository = SkillStopStatusRepository(session)

    async def read(self, user_id: UUID, operation_id: UUID) -> SkillStopStatus:
        """
        只有准确终止凭据能够确认本地冻结，最新发布状态覆盖历史任务提示。

        :param user_id (UUID): 认证用户身份
        :param operation_id (UUID): 原始快照兼操作身份
        :return SkillStopStatus: 权威保存进度
        """
        rows = await self.repository.read(user_id, operation_id)
        if rows is None:
            raise SkillContentError("SKILL_NOT_FOUND", "finalization operation not found")
        snapshot, termination, finalization, publication, original = rows
        status = "local_durable" if termination is not None else "awaiting_node"
        if termination is not None and termination.incoming_digest is None:
            status = "capture_pending"
        if finalization is not None:
            status = finalization.status
        if publication is not None:
            status = publication.status
        return SkillStopStatus(
            operation_id=snapshot.id,
            session_id=snapshot.session_reference_id,
            account_id=snapshot.account_id,
            process_status=original.status if original is not None else "deleted",
            process_stopped=termination is not None,
            status=cast(SaveStatus, status),
            capture_error=cast(CaptureError, termination.capture_error)
            if status == "capture_pending" and termination is not None
            else None,
            unclean=termination.unclean
            if termination is not None
            else finalization.unclean
            if finalization is not None
            else None,
            finalization_id=finalization.id if finalization is not None else None,
            checkpoint_id=finalization.checkpoint_id if finalization is not None else None,
            publication_id=publication.id if publication is not None else None,
            content_retained=finalization is not None
            and finalization.checkpoint_id is not None
            and finalization.content_retired_at is None,
        )
