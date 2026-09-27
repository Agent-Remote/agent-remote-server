"""
仅以已提交的初始目录凭据确认接管任务，保留所有未完成捕获的重试机会。
"""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.repositories.nodes import NodeRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.repositories.skill_takeover import SkillTakeoverRepository
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.takeover_context import content_hash, takeover_payload


class TakeoverResultGuard:
    """
    通用结果不能替代目录权威事务，也不能消耗仍需重试的预约。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定调用方事务。

        :param session (AsyncSession): 当前数据库事务
        """
        self.session = session

    async def authorize(
        self, task: NodeTask, result: dict[str, object], outcome: Literal["succeeded", "failed"]
    ) -> bool:
        """
        先锁定原所有者再核实任务与初始检查点，精确重放不触发生命周期变更。

        :param task (NodeTask): 已认证节点任务
        :param result (dict[str, object]): 节点上报的有界结果
        :param outcome (Literal["succeeded", "failed"]): 通用接口结果类型
        :return bool: 是否为已接受的精确结果重放
        """
        receipt = await self.session.scalar(
            select(SkillAccountTakeover).where(SkillAccountTakeover.task_id == task.id)
        )
        if receipt is None:
            if task.task_type == "takeover_tool_account_skills":
                raise SkillContentError("TAKEOVER_RESULT_INVALID", "takeover reservation is absent")
            return False
        await SkillStorageRepository(self.session).lock_usage(receipt.user_id)
        receipt = await SkillTakeoverRepository(self.session).on_node(task.node_id, receipt.id)
        locked = await SkillRuntimeRepository(self.session).task(task.node_id, task.id)
        if (
            receipt is None
            or locked is None
            or locked.task_type != "takeover_tool_account_skills"
            or locked.task_id != f"takeover_tool_account_skills:{receipt.id}"
            or content_hash(locked.payload) != content_hash(takeover_payload(receipt))
            or outcome != "succeeded"
        ):
            raise SkillContentError("TAKEOVER_RESULT_INVALID", "takeover result identity changed")
        if receipt.status != "committed" or receipt.checkpoint_id is None:
            raise SkillContentError("STATE_PENDING", "initial directory is not yet committed")
        expected = {
            "status": "committed",
            "takeover_id": str(receipt.id),
            "task_record_id": str(receipt.task_id),
            "tool_account_id": str(receipt.account_id),
            "checkpoint_id": str(receipt.checkpoint_id),
            "capture_digest": receipt.capture_digest,
        }
        if result != expected:
            raise SkillContentError(
                "TAKEOVER_RESULT_INVALID", "takeover result differs from commit"
            )
        saved = await NodeRepository(self.session).get_task_result(locked.task_id)
        if saved is not None:
            if (
                saved.node_task_id != locked.id
                or locked.status != "succeeded"
                or saved.status != "succeeded"
                or saved.result != expected
                or saved.error is not None
            ):
                raise SkillContentError("TAKEOVER_RESULT_CONFLICT", "takeover result changed")
            return True
        if locked.status not in {"leased", "running"}:
            raise SkillContentError("TAKEOVER_RESULT_CONFLICT", "takeover task is not active")
        return False
