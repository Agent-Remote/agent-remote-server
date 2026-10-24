"""
在删除历史前拒绝尚未解决的停止，不将旧成功收据升级为物理证明。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.errors import ApiError
from agent_remote_server.models import Session
from agent_remote_server.repositories.session_deletion import SessionDeletionRepository


async def require_settled_session_stops(session: AsyncSession, runtime: Session) -> None:
    """
    调用方已持有历史与会话锁，所有原停止任务必须有一致的成功收据。

    :param session (AsyncSession): 删除事务
    :param runtime (Session): 已锁定并刷新的原会话
    :raises ApiError: 存在未解决或不能绑定原成功收据的停止任务
    """
    repository = SessionDeletionRepository(session)
    for task in await repository.stop_tasks(runtime):
        receipts = await repository.receipts(task)
        valid_task = (
            task.task_type == "stop_tool_session"
            and task.node_id == runtime.node_id
            and task.payload.get("session_id") == str(runtime.id)
            and task.status == "succeeded"
            and (
                "runtime_backend" not in task.payload
                or task.payload["runtime_backend"] == runtime.runtime_backend
            )
        )
        if not valid_task or len(receipts) != 1:
            raise unresolved_stop()
        receipt = receipts[0]
        value = receipt.result
        if (
            receipt.node_task_id != task.id
            or receipt.task_id != task.task_id
            or receipt.status != "succeeded"
            or receipt.error is not None
            or not isinstance(value, dict)
            or value.get("status") != "stopped"
            or value.get("session_id") != str(runtime.id)
            or "runtime_backend" in value
            and value["runtime_backend"] != runtime.runtime_backend
        ):
            raise unresolved_stop()


def unresolved_stop() -> ApiError:
    """
    返回不泄露原始结果内容的停止未解决错误。

    :return ApiError: 阻止历史删除的固定错误
    """
    return ApiError(
        code="SESSION_STOP_UNRESOLVED",
        message="Session stop is unresolved; history cannot be deleted.",
        status_code=409,
    )
