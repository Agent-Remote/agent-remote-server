"""
在任务租约锁之前按用户独立提交后台预约，已有执行只能由原排空协议结束。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.repositories.skill_deployment_scheduling import (
    DeploymentCandidate,
    deployment_candidates,
)
from agent_remote_server.repositories.skill_deployment_tasks import SkillDeploymentTaskRepository
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_attempts import (
    current_attempts,
    save_projection,
)
from agent_remote_server.services.skills.deployment_capability import supports_deployment
from agent_remote_server.services.skills.deployment_dispatch import SkillDeploymentDispatch
from agent_remote_server.services.skills.retention.clocks import retention_mutation

_WAITING = frozenset(
    {
        "SKILL_MANAGER_DISABLED",
        "SKILL_MANAGER_UNSUPPORTED",
        "MIGRATION_PENDING",
        "STATE_WRITERS_CHANGED",
        "DEPLOYMENT_TASK_CHANGED",
    }
)
_TERMINAL = frozenset(
    {
        "AUTHORIZATION_DENIED",
        "ACCOUNT_NOT_AVAILABLE",
        "DEPLOYMENT_BINDING_CHANGED",
        "DEPLOYMENT_PLAN_CHANGED",
        "TAKEOVER_RECOVERY_REQUIRED",
        "STATE_WRITERS_UNKNOWN",
        "STATE_EXPIRED",
        "STATE_PRECONDITION_CHANGED",
        "INVALID_SKILL_FORMAT",
        "SKILL_SOURCE_CONFLICT",
        "STATE_SCOPE_MISMATCH",
        "STATE_DEPENDENCY_MISSING",
        "REVISION_EXPIRED",
        "UNSUPPORTED_TOOL",
        "QUOTA_EXCEEDED",
    }
)


async def schedule_deployments(session: AsyncSession, settings: Settings, node_id: UUID) -> None:
    """
    每轮最多检查四个原目标，提交后才允许普通任务轮询取得任务锁。

    :param session (AsyncSession): 普通轮询事务，调用前不能持有任务锁
    :param settings (Settings): 当前服务策略
    :param node_id (UUID): 已认证节点
    """
    if not settings.skill_manager_enabled:
        return
    candidates = await deployment_candidates(session, node_id, 4)
    if not candidates:
        return
    await session.commit()
    for candidate in candidates:
        try:
            await _schedule(session, settings, candidate)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _schedule(
    session: AsyncSession, settings: Settings, candidate: DeploymentCandidate
) -> None:
    """
    锁内重新加载原尝试及任务库存，过期候选不会修改后来执行。

    :param session (AsyncSession): 本目标独立事务
    :param settings (Settings): 当前部署策略
    :param candidate (DeploymentCandidate): 锁外读取的原身份
    """
    async with retention_mutation(session, candidate.user_id):
        library = SkillLibraryRepository(session)
        operation = await library.operation(candidate.user_id, candidate.operation_id)
        if operation is None:
            return
        current = current_attempts(
            operation,
            await SkillDeploymentAttemptRepository(session).attempts(
                candidate.user_id, operation.id
            ),
        )
        attempt = current.get(candidate.account_id)
        if (
            attempt is None
            or attempt.id != candidate.attempt_id
            or attempt.status not in {"pending", "needs_resolution"}
        ):
            return
        bindings = await SkillDeploymentTaskRepository(session).bindings(
            candidate.user_id, operation.id, candidate.account_id
        )
        if any(binding.attempt_id == attempt.id for binding in bindings):
            return
        attempt.updated_at = datetime.now(UTC)
        if operation.replacement_id is not None:
            attempt.status, attempt.error_code = "superseded", "OPERATION_SUPERSEDED"
            save_projection(operation, current)
            return
        original = attempt.status, attempt.error_code
        attempt.status, attempt.error_code = "pending", None
        save_projection(operation, current)
        try:
            await SkillDeploymentDispatch(session, settings).reserve(
                candidate.user_id, operation.id, candidate.account_id, attempt.id
            )
        except SkillContentError as error:
            if error.code == "SKILL_MANAGER_UNSUPPORTED" and not await _compatible(
                session, settings, candidate
            ):
                attempt.status, attempt.error_code = "unsupported", error.code
            elif error.code == "STATE_MIGRATION_REQUIRED":
                attempt.status, attempt.error_code = "needs_resolution", error.code
            elif error.code in _WAITING:
                attempt.status, attempt.error_code = original
            elif error.code in _TERMINAL:
                attempt.status, attempt.error_code = "failed", error.code
                attempt.retryable = error.code == "QUOTA_EXCEEDED"
            else:
                raise
            save_projection(operation, current)


async def _compatible(
    session: AsyncSession, settings: Settings, candidate: DeploymentCandidate
) -> bool:
    """
    区分已知兼容但离线的等待与真实能力撤回，不因心跳过期写入不支持。

    :param session (AsyncSession): 已持有原用户锁的事务
    :param settings (Settings): 当前部署策略
    :param candidate (DeploymentCandidate): 已核对原绑定的目标
    :return bool: 已保存报告是否仍满足受理协议
    """
    account = await SkillLibraryRepository(session).account(candidate.user_id, candidate.account_id)
    if account is None or account.affinity_node_id is None:
        return False
    node = await SkillDeploymentTaskRepository(session).node(account.affinity_node_id)
    return node is not None and supports_deployment(
        node, account.runtime_backend, account.tool_type, settings, fresh=False
    )
