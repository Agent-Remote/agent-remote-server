"""
核对原配置、精确当前尝试与实时后端授权，不从代数变化猜测输入。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.models import Node, ToolAccount
from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.repositories.skill_deployment_tasks import SkillDeploymentTaskRepository
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.schemas.skill_deployment import SkillDeploymentPlan
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_attempts import current_attempts
from agent_remote_server.services.skills.deployment_capability import supports_deployment
from agent_remote_server.services.skills.deployment_discovery import execution_plans
from agent_remote_server.services.skills.deployment_plans import resolve_plan
from agent_remote_server.services.skills.deployment_validation import saved_plans

TASK_TYPE = "prepare_account_skills"


@dataclass(frozen=True)
class DeploymentAuthority:
    """
    用户锁内验证的原始配置与当前尝试，不能作为跨事务权限缓存。
    """

    operation: SkillOperation
    account: ToolAccount
    node: Node
    plan: SkillDeploymentPlan
    current: dict[UUID, SkillDeploymentAttempt]


async def deployment_authority(
    session: AsyncSession,
    settings: Settings,
    user_id: UUID,
    operation_id: UUID,
    account_id: UUID,
    attempt_id: UUID,
) -> DeploymentAuthority:
    """
    拒绝旧尝试、替代配置、账户变化及不完整能力，保持原输入的严格边界。

    :param session (AsyncSession): 已持有用户锁的事务
    :param settings (Settings): 服务配置
    :param user_id (UUID): 原所有者
    :param operation_id (UUID): 原操作
    :param account_id (UUID): 原账户
    :param attempt_id (UUID): 精确当前尝试
    :return DeploymentAuthority: 本事务的完整授权证据
    """
    if not settings.skill_manager_enabled:
        raise SkillContentError("SKILL_MANAGER_DISABLED", "skill manager is disabled")
    library = SkillLibraryRepository(session)
    operation = await library.operation(user_id, operation_id)
    if operation is None:
        raise SkillContentError("OPERATION_NOT_FOUND", "operation not found")
    if not await SkillDeploymentTaskRepository(session).owner_active(user_id):
        raise SkillContentError("AUTHORIZATION_DENIED", "deployment owner is not active")
    targets, entries = await SkillDeploymentRepository(session).rows(user_id, operation_id)
    plans = await execution_plans(session, saved_plans(operation, targets, entries))
    current = current_attempts(
        operation, await SkillDeploymentAttemptRepository(session).attempts(user_id, operation_id)
    )
    attempt = current.get(account_id)
    if attempt is None or attempt.id != attempt_id:
        raise SkillContentError("ATTEMPT_CHANGED", "deployment attempt changed")
    if operation.replacement_id is not None or operation.status == "superseded":
        raise SkillContentError("OPERATION_SUPERSEDED", "deployment operation was replaced")
    if attempt.status not in {"pending", "running"}:
        raise SkillContentError("DEPLOYMENT_NOT_ACTIVE", "deployment attempt is not active")
    plan = next(plan for plan in plans if plan.account_id == account_id)
    account = await library.account(user_id, account_id)
    if account is None or account.status != "active":
        raise SkillContentError("ACCOUNT_NOT_AVAILABLE", "deployment account is unavailable")
    if plan.node_id is None or (
        account.affinity_node_id,
        account.runtime_backend,
        account.tool_type,
    ) != (plan.node_id, plan.runtime_backend, plan.tool_type):
        raise SkillContentError("DEPLOYMENT_BINDING_CHANGED", "original deployment binding changed")
    if (await resolve_plan(library, operation, account)).digest() != plan.digest():
        raise SkillContentError("DEPLOYMENT_PLAN_CHANGED", "original deployment selection changed")
    node = await SkillDeploymentTaskRepository(session).node(plan.node_id)
    if node is None or not supports_deployment(
        node, plan.runtime_backend, plan.tool_type, settings, fresh=True
    ):
        raise SkillContentError(
            "SKILL_MANAGER_UNSUPPORTED", "node lacks deployment protocol support"
        )
    return DeploymentAuthority(operation, account, node, plan, current)


def deployment_payload(binding: SkillDeploymentTask) -> dict[str, object]:
    """
    任务仅携带固定身份和摘要，不包含清单、私有路径或文件内容。

    :param binding (SkillDeploymentTask): 不可变任务绑定
    :return dict[str, object]: 唯一规范任务载荷
    """
    return {
        "protocol_version": 1,
        "user_id": str(binding.user_id),
        "operation_id": str(binding.operation_id),
        "tool_account_id": str(binding.account_id),
        "attempt_id": str(binding.attempt_id),
        "task_record_id": str(binding.task_id),
        "checkpoint_id": str(binding.checkpoint_id),
        "tree_digest": binding.content_digest,
        "plan_digest": binding.plan_digest,
        "runtime_backend": "native",
    }
