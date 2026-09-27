"""
在原始配置计划上原子追加精确重试，不重复成功项或重新抓取来源。
"""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_deployment_attempts import (
    SkillDeploymentAttempt,
    SkillDeploymentRetry,
)
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.schemas.skill_deployment_retry import SkillDeploymentRetryRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.deployment_attempts import (
    current_attempts,
    save_projection,
)
from agent_remote_server.services.skills.deployment_discovery import execution_plans
from agent_remote_server.services.skills.deployment_plans import resolve_plan
from agent_remote_server.services.skills.deployment_validation import saved_plans
from agent_remote_server.services.skills.retention.clocks import retention_mutation


async def retry_deployment(
    session: AsyncSession, user_id: UUID, operation_id: UUID, request: SkillDeploymentRetryRequest
) -> SkillOperation:
    """
    先检查完整选定集合再追加，失去响应后只恢复同一已提交重试身份。

    :param session (AsyncSession): 调用方提交的用户事务
    :param user_id (UUID): 认证所有者
    :param operation_id (UUID): 原始配置操作
    :param request (SkillDeploymentRetryRequest): 原代数与精确失败尝试集合
    :return SkillOperation: 当前原操作，配置身份和已成功目标保持不变
    """
    raw = request.model_dump(mode="json") | {"operation_id": str(operation_id)}
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    library = SkillLibraryRepository(session)
    repository = SkillDeploymentAttemptRepository(session)
    if await library.operation(user_id, operation_id) is None:
        raise SkillContentError("OPERATION_NOT_FOUND", "operation not found")
    async with retention_mutation(session, user_id):
        await library.lock_library(user_id)
        operation = await library.operation(user_id, operation_id)
        if operation is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "operation not found")
        targets, entries = await SkillDeploymentRepository(session).rows(user_id, operation_id)
        plans = {
            plan.account_id: plan
            for plan in await execution_plans(session, saved_plans(operation, targets, entries))
        }
        current = current_attempts(operation, await repository.attempts(user_id, operation_id))
        previous = await repository.retry_by_key(user_id, request.idempotency_key)
        if previous is not None:
            if previous.operation_id != operation_id or previous.request_digest != digest:
                raise SkillContentError(
                    "IDEMPOTENCY_CONFLICT", "retry key belongs to another request"
                )
            return operation
        if operation.status == "superseded" or operation.replacement_id is not None:
            raise SkillContentError("OPERATION_SUPERSEDED", "original operation was replaced")
        if operation.generation != request.expected_generation:
            raise SkillContentError(
                "GENERATION_CONFLICT", "retry does not match original generation"
            )
        if (
            operation.attempts_version != 1
            or operation.status != "failed"
            or not operation.retryable
        ):
            raise SkillContentError(
                "OPERATION_NOT_RETRYABLE", "original operation cannot be retried"
            )
        selected = []
        for target in request.targets:
            attempt = current.get(target.account_id)
            if attempt is None or attempt.id != target.attempt_id:
                raise SkillContentError("ATTEMPT_CHANGED", "original target attempt changed")
            if attempt.status != "failed" or not attempt.retryable:
                raise SkillContentError("TARGET_NOT_RETRYABLE", "target cannot be retried")
            plan = plans[target.account_id]
            if plan.node_id is None or plan.runtime_backend is None:
                raise SkillContentError(
                    "TARGET_NOT_RETRYABLE", "original target has no runtime binding"
                )
            account = await library.account(user_id, target.account_id)
            if account is None or account.status != "active":
                raise SkillContentError("ACCOUNT_NOT_AVAILABLE", "original account is unavailable")
            if (
                account.affinity_node_id != plan.node_id
                or account.runtime_backend != plan.runtime_backend
                or account.tool_type != plan.tool_type
            ):
                raise SkillContentError(
                    "DEPLOYMENT_BINDING_CHANGED", "original target binding changed"
                )
            if (await resolve_plan(library, operation, account)).digest() != plan.digest():
                raise SkillContentError(
                    "DEPLOYMENT_PLAN_CHANGED", "original effective selection changed"
                )
            selected.append(attempt)
        for attempt in selected:
            successor = SkillDeploymentAttempt(
                id=uuid4(),
                user_id=user_id,
                operation_id=operation_id,
                account_id=attempt.account_id,
                number=attempt.number + 1,
                predecessor_id=attempt.id,
                plan_digest=attempt.plan_digest,
                status="pending",
                retryable=False,
                error_code=None,
            )
            session.add(successor)
            current[attempt.account_id] = successor
        session.add(
            SkillDeploymentRetry(
                user_id=user_id,
                operation_id=operation_id,
                idempotency_key=request.idempotency_key,
                request_digest=digest,
            )
        )
        save_projection(operation, current)
        await session.flush()
        return operation
