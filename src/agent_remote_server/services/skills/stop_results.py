"""
阻止受管停止结果绕过原始冻结凭据或把异常退出改写为干净停止。
"""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination
from agent_remote_server.repositories.nodes import NodeRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.services.skills.content import SkillContentError


class ManagedStopResultGuard:
    """
    仅验证独立终止事务已提交的证据，不在任务完成事务内部提交收尾状态。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定外层历史变更事务。

        :param session (AsyncSession): 节点结果请求事务
        """
        self.session = session

    async def authorize(
        self, task: NodeTask, result: dict[str, object], outcome: Literal["succeeded", "failed"]
    ) -> bool:
        """
        从持久快照识别受管停止，即使任务标记丢失也不能走旧的完成分支。

        :param task (NodeTask): 已认证节点任务
        :param result (dict[str, object]): 有界原始结果
        :param outcome (Literal["succeeded", "failed"]): 通用接口结果类型
        :return bool: 是否已接受同一不可变结果
        """
        if task.task_type != "stop_tool_session":
            return False
        marked = any(key.casefold() == "skill_finalization" for key in task.payload)
        reference = task.task_id.removeprefix("stop_tool_session:")
        try:
            session_id = UUID(reference)
        except ValueError:
            session_id = None
        try:
            payload_session = UUID(str(task.payload.get("session_id", "")))
        except ValueError:
            payload_session = None
        identities = {value for value in (session_id, payload_session) if value is not None}
        snapshots = (
            await self.session.scalars(
                select(SessionSkillSnapshot).where(
                    SessionSkillSnapshot.session_reference_id.in_(identities),
                    SessionSkillSnapshot.node_id == task.node_id,
                )
            )
        ).all()
        if not snapshots:
            if marked:
                raise SkillContentError("SKILL_STOP_RESULT_INVALID", "invalid managed stop")
            return False
        if (
            len(snapshots) != 1
            or session_id is None
            or payload_session != session_id
            or task.payload.get("session_id") != str(session_id)
            or task.task_id != f"stop_tool_session:{session_id}"
            or outcome != "succeeded"
        ):
            raise SkillContentError(
                "SKILL_STOP_RESULT_INVALID", "managed stop requires original identity"
            )
        snapshot = snapshots[0]
        locked = await SkillRuntimeRepository(self.session).task(task.node_id, task.id)
        if locked is None:
            raise SkillContentError("SKILL_STOP_RESULT_INVALID", "managed stop task is unavailable")
        termination = await self.session.get(SkillSnapshotTermination, snapshot.id)
        if termination is None:
            raise SkillContentError("STATE_PENDING", "original termination is not yet confirmed")
        incoming = result.get("incoming_digest")
        if incoming is not None and (
            type(incoming) is not str or incoming != termination.incoming_digest
        ):
            raise SkillContentError("SKILL_STOP_RESULT_INVALID", "managed stop digest differs")
        expected = {
            "status": "stopped",
            "session_id": str(session_id),
            "runtime_backend": "native",
            "skill_finalization_operation_id": str(snapshot.id),
            "incoming_digest": incoming,
            "unclean": termination.unclean,
        }
        if (
            snapshot.runtime_backend != "native"
            or result != expected
            or type(result.get("unclean")) is not bool
        ):
            raise SkillContentError(
                "SKILL_STOP_RESULT_INVALID", "managed stop differs from frozen input"
            )
        receipt = await NodeRepository(self.session).get_task_result(task.task_id)
        if receipt is not None:
            if (
                receipt.node_task_id != locked.id
                or locked.status != "succeeded"
                or receipt.status != "succeeded"
                or receipt.result != expected
                or receipt.error is not None
            ):
                raise SkillContentError("SKILL_STOP_RESULT_CONFLICT", "managed stop result changed")
            return True
        if locked.status in {"cancelled", "expired", "failed", "succeeded"}:
            raise SkillContentError("SKILL_STOP_RESULT_CONFLICT", "managed stop task is terminal")
        return False
