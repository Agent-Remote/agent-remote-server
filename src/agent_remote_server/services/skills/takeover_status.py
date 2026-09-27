"""
解释已提交的接管阶段，查询不续租、不修复也不创建新任务。
"""

from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_takeover_status import SkillTakeoverStatusRepository
from agent_remote_server.schemas.skill_takeover_status import SkillTakeoverStatus, TakeoverPhase
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.takeover_context import content_hash, takeover_payload


class SkillTakeoverStatusService:
    """
    只显示 Server 已有证据，不能从任务终态推断本地捕获或写入者退出。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        创建只读仓储。

        :param session (AsyncSession): 当前请求事务
        """
        self.repository = SkillTakeoverStatusRepository(session)

    async def read(self, user_id: UUID, operation_id: UUID) -> SkillTakeoverStatus:
        """
        未提交预约的终态任务需要恢复，原始已提交权威不会因确认延迟退回等待。

        :param user_id (UUID): 已认证用户
        :param operation_id (UUID): 原始接管身份
        :return SkillTakeoverStatus: 不含文件、宿主路径或写入者身份的状态
        """
        rows = await self.repository.read(user_id, operation_id)
        if rows is None:
            raise SkillContentError("SKILL_NOT_FOUND", "takeover operation not found")
        receipt, task = rows
        valid_task = (
            task is not None
            and task.task_type == "takeover_tool_account_skills"
            and task.task_id == f"takeover_tool_account_skills:{receipt.id}"
            and content_hash(task.payload) == content_hash(takeover_payload(receipt))
        )
        return SkillTakeoverStatus(
            operation_id=receipt.id,
            account_id=receipt.account_id,
            status=cast(TakeoverPhase, receipt.status),
            task_status=task.status if task is not None else "missing",
            checkpoint_id=receipt.checkpoint_id,
            recovery_required=not valid_task
            or receipt.status != "committed"
            and (task is None or task.status not in {"pending", "leased", "running"}),
        )
