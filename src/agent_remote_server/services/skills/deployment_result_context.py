"""
成功与撤权共用原用户后任务的锁顺序，不依赖当前内容或新准入能力。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.repositories.skill_deployment_tasks import SkillDeploymentTaskRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_deployment_content import SkillDeploymentIdentity
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_context import TASK_TYPE, deployment_payload
from agent_remote_server.services.skills.takeover_context import content_hash


def deployment_identity(binding: SkillDeploymentTask) -> SkillDeploymentIdentity:
    """
    仅从持久任务绑定还原有界身份，不读取或刷新原始内容。

    :param binding (SkillDeploymentTask): 锁内原始绑定
    :return SkillDeploymentIdentity: 原始完整身份
    """
    return SkillDeploymentIdentity(
        operation_id=binding.operation_id,
        attempt_id=binding.attempt_id,
        task_id=binding.task_id,
        user_id=binding.user_id,
        account_id=binding.account_id,
        node_id=binding.node_id,
        checkpoint_id=binding.checkpoint_id,
        plan_digest=binding.plan_digest,
        tree_digest=binding.content_digest,
        runtime_backend="native",
    )


async def original_deployment(
    session: AsyncSession, node_id: UUID, task_id: UUID, attempt_id: UUID
) -> tuple[SkillDeploymentTask, NodeTask]:
    """
    原归属先发现后加锁重读，准备成功与撤权必须在相同锁内竞争。

    :param session (AsyncSession): 当前请求事务
    :param node_id (UUID): 已认证原节点
    :param task_id (UUID): 原任务数据库身份
    :param attempt_id (UUID): 原部署尝试
    :return tuple[SkillDeploymentTask, NodeTask]: 锁内原始绑定及任务
    """
    repository = SkillDeploymentTaskRepository(session)
    binding = await repository.for_task(task_id)
    if binding is None or binding.node_id != node_id or binding.attempt_id != attempt_id:
        raise SkillContentError("DEPLOYMENT_NOT_FOUND", "deployment task not found")
    if await SkillStorageRepository(session).lock_existing_usage(binding.user_id) is None:
        raise SkillContentError("DEPLOYMENT_NOT_FOUND", "deployment task not found")
    binding = await repository.for_task(task_id)
    if binding is None or binding.node_id != node_id or binding.attempt_id != attempt_id:
        raise SkillContentError("DEPLOYMENT_NOT_FOUND", "deployment task not found")
    if not await repository.owner_active(binding.user_id):
        raise SkillContentError("AUTHORIZATION_DENIED", "deployment owner is not active")
    task = await SkillRuntimeRepository(session).task(node_id, task_id)
    if (
        task is None
        or task.task_type != TASK_TYPE
        or task.task_id != f"{TASK_TYPE}:{attempt_id}"
        or content_hash(task.payload) != content_hash(deployment_payload(binding))
    ):
        raise SkillContentError("DEPLOYMENT_RESULT_INVALID", "deployment result binding differs")
    return binding, task
