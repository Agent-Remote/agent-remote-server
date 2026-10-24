"""
为旧会话任务固定首次终态收据，禁止重复或冲突回报再次应用业务副作用。
"""

import json
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.errors import ApiError
from agent_remote_server.models import NodeTask
from agent_remote_server.repositories.session_task_results import SessionTaskResultRepository


class SessionTaskResultGuard:
    """
    在既有用户历史锁之后锁定原会话和任务，不替代受管任务专用授权。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        复用外层提交事务。

        :param session (AsyncSession): 原请求事务
        """
        self.repository = SessionTaskResultRepository(session)

    async def replay(
        self, task: NodeTask, value: dict[str, object], outcome: Literal["succeeded", "failed"]
    ) -> bool:
        """
        相同终态仅返回原结果，冲突或没有收据的终态不能重新结算。

        :param task (NodeTask): 已认证节点的原任务
        :param value (dict[str, object]): 已经过既有安全规范化的结果
        :param outcome (Literal["succeeded", "failed"]): 本次回报结果类型
        :return bool: 是否精确重放已有收据
        """
        if task.task_type not in {"create_tool_session", "stop_tool_session"}:
            return False
        record_id, node_id, task_id, kind = task.id, task.node_id, task.task_id, task.task_type
        original_session = task.payload.get("session_id")
        try:
            session_id = UUID(str(original_session))
        except ValueError:
            raise result_conflict() from None
        locked = await self.repository.lock_task(session_id, record_id, node_id, task_id, kind)
        if locked is None or locked.payload.get("session_id") != original_session:
            raise result_conflict()
        saved = await self.repository.receipt(task_id)
        if saved is None:
            if locked.status in {"succeeded", "failed", "cancelled", "expired"}:
                raise result_conflict()
            return False
        expected = saved.result if outcome == "succeeded" else saved.error
        other = saved.error if outcome == "succeeded" else saved.result
        if (
            saved.node_task_id != record_id
            or saved.status != outcome
            or locked.status != outcome
            or other is not None
            or json.dumps(expected, sort_keys=True) != json.dumps(value, sort_keys=True)
        ):
            raise result_conflict()
        return True


def result_conflict() -> ApiError:
    """
    返回不泄露历史任务结果的固定冲突。

    :return ApiError: 原任务收据不匹配
    """
    return ApiError(
        code="SESSION_TASK_RESULT_CONFLICT",
        message="Session task result conflicts with its original receipt.",
        status_code=409,
    )
