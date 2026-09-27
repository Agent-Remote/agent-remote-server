"""
按原始部署任务与当前领取租约授权，通用任务结果不能替代部署协议。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.repositories.skill_deployment_tasks import SkillDeploymentTaskRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_context import (
    TASK_TYPE,
    deployment_authority,
    deployment_payload,
)
from agent_remote_server.services.skills.deployment_input import validate_input
from agent_remote_server.services.skills.takeover_context import content_hash


async def authorize_deployment(
    session: AsyncSession,
    settings: Settings,
    node_id: UUID,
    task_id: UUID,
    attempt_id: UUID,
    lease_attempt: int,
) -> SkillDeploymentTask:
    """
    原用户锁之后重读绑定、租约、配置和纪元，返回值不能跨事务缓存为权限。

    :param session (AsyncSession): 内容或进度请求事务
    :param settings (Settings): 服务配置
    :param node_id (UUID): 已认证节点
    :param task_id (UUID): 精确数据库任务身份
    :param attempt_id (UUID): 原部署尝试
    :param lease_attempt (int): 当前轮询领取次数
    :return SkillDeploymentTask: 本次仍可消费的精确输入绑定
    """
    repository = SkillDeploymentTaskRepository(session)
    binding = await repository.for_task(task_id)
    if binding is None or binding.node_id != node_id or binding.attempt_id != attempt_id:
        raise SkillContentError("DEPLOYMENT_NOT_FOUND", "deployment task not found")
    await SkillStorageRepository(session).lock_usage(binding.user_id)
    binding = await repository.for_task(task_id)
    if binding is None or binding.node_id != node_id or binding.attempt_id != attempt_id:
        raise SkillContentError("DEPLOYMENT_NOT_FOUND", "deployment task not found")
    if await repository.termination(attempt_id) is not None:
        raise SkillContentError(
            "DEPLOYMENT_REVOKED", "original deployment requires permanent drain"
        )
    authority = await deployment_authority(
        session, settings, binding.user_id, binding.operation_id, binding.account_id, attempt_id
    )
    task = await SkillRuntimeRepository(session).task(node_id, task_id)
    if (
        task is None
        or task.task_type != TASK_TYPE
        or task.task_id != f"{TASK_TYPE}:{attempt_id}"
        or content_hash(task.payload) != content_hash(deployment_payload(binding))
        or task.status not in {"leased", "running"}
        or task.lease_until is None
        or task.lease_until.replace(tzinfo=task.lease_until.tzinfo or UTC) <= datetime.now(UTC)
        or type(lease_attempt) is not int
        or lease_attempt < 1
        or task.retry_count != lease_attempt
    ):
        raise SkillContentError("DEPLOYMENT_LEASE_CHANGED", "deployment task lease is not current")
    await validate_input(session, authority, binding)
    return binding


async def reject_generic_deployment_result(session: AsyncSession, task: NodeTask) -> None:
    """
    专用结果协议接入前只拒绝，不允许通用完成或失败误释放内容及执行权。

    :param session (AsyncSession): 原任务结果事务
    :param task (NodeTask): 已认证节点的任务
    """
    if task.task_type == TASK_TYPE or await SkillDeploymentTaskRepository(session).for_task(
        task.id
    ):
        raise SkillContentError(
            "DEPLOYMENT_RESULT_REQUIRED", "dedicated deployment result is required"
        )
