"""
验证完整部署尝试链并维护与原始计划分离的当前状态投影。
"""

from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.schemas.skill_results import SkillMutationData, SkillOperationTarget

RETRYABLE_ERRORS = frozenset(
    {"NODE_UNAVAILABLE", "TRANSFER_FAILED", "QUOTA_EXCEEDED", "DEPLOYMENT_INTERRUPTED"}
)
ATTEMPT_STATES = frozenset(
    {
        "stored",
        "unsupported",
        "pending",
        "running",
        "ready",
        "needs_resolution",
        "failed",
        "superseded",
    }
)


async def initial_attempts(
    session: AsyncSession, operation: SkillOperation, targets: list[SkillOperationTarget]
) -> None:
    """
    受理时追加首个目标观察，不将存储或不支持状态改写为执行就绪。

    :param session (AsyncSession): 配置受理保存点
    :param operation (SkillOperation): 已有原始计划的操作
    :param targets (list[SkillOperationTarget]): 待补充尝试身份的原目标
    """
    operation.attempts_version = 1
    for target in targets:
        if target.plan_digest is None or target.readiness not in {
            "stored",
            "unsupported",
            "pending",
        }:
            raise ValueError("initial deployment attempt lacks a classified acceptance")
        identity = uuid4()
        session.add(
            SkillDeploymentAttempt(
                id=identity,
                user_id=operation.user_id,
                operation_id=operation.id,
                account_id=target.account_id,
                number=1,
                predecessor_id=None,
                plan_digest=target.plan_digest,
                status=target.readiness,
                retryable=False,
                error_code=target.error_code,
            )
        )
        target.attempt_id, target.attempt_number = identity, 1
    await session.flush()


def current_attempts(
    operation: SkillOperation, attempts: Sequence[SkillDeploymentAttempt]
) -> dict[UUID, SkillDeploymentAttempt]:
    """
    从完整链核对最新投影，拒绝跳号、外来前序、状态漂移或遗失的目标。

    :param operation (SkillOperation): 已授权原始操作
    :param attempts (Sequence[SkillDeploymentAttempt]): 全部已保存尝试
    :return dict[UUID, SkillDeploymentAttempt]: 每个原目标唯一的当前尝试
    """
    data = SkillMutationData.model_validate(operation.result_json)
    if operation.attempts_version is None:
        if attempts or any(
            target.attempt_id is not None or target.attempt_number is not None or target.retryable
            for target in data.targets
        ):
            raise ValueError("legacy operation has unexpected deployment attempts")
        return {}
    if operation.attempts_version != 1 or operation.plan_version != 1:
        raise ValueError("unclassified deployment attempts version")
    targets = {target.account_id: target for target in data.targets}
    grouped: dict[UUID, list[SkillDeploymentAttempt]] = defaultdict(list)
    for attempt in attempts:
        if (
            attempt.user_id != operation.user_id
            or attempt.operation_id != operation.id
            or attempt.account_id not in targets
        ):
            raise ValueError("deployment attempt belongs to another target")
        grouped[attempt.account_id].append(attempt)
    if len(targets) != len(data.targets) or grouped.keys() != targets.keys():
        raise ValueError("deployment attempt inventory is incomplete")
    current = {}
    for account, rows in grouped.items():
        previous = None
        for number, attempt in enumerate(sorted(rows, key=lambda row: row.number), 1):
            if (
                attempt.number != number
                or attempt.predecessor_id != (previous.id if previous is not None else None)
                or attempt.plan_digest != targets[account].plan_digest
                or attempt.status not in ATTEMPT_STATES
                or (attempt.status in {"stored", "pending", "running", "ready"})
                != (attempt.error_code is None)
                or attempt.retryable
                and (attempt.status != "failed" or attempt.error_code not in RETRYABLE_ERRORS)
                or previous is not None
                and (previous.status != "failed" or not previous.retryable)
            ):
                raise ValueError("deployment attempt chain is inconsistent")
            previous = attempt
        assert previous is not None
        current[account] = previous
    projected, status, retryable = attempt_projection(operation, current)
    if projected != data or operation.status != status or operation.retryable != retryable:
        raise ValueError("deployment attempt projection is inconsistent")
    return current


def attempt_projection(
    operation: SkillOperation, current: dict[UUID, SkillDeploymentAttempt]
) -> tuple[SkillMutationData, str, bool]:
    """
    只从精确当前尝试生成状态，不重新解析来源或修改已保存的配置计划。

    :param operation (SkillOperation): 原始配置受理
    :param current (dict[UUID, SkillDeploymentAttempt]): 完整目标当前尝试
    :return tuple[SkillMutationData, str, bool]: 当前目标投影、操作阶段与可重试标志
    """
    data = SkillMutationData.model_validate(operation.result_json)
    data.replacement_id = operation.replacement_id
    for index, target in enumerate(data.targets):
        row = current[target.account_id]
        readiness = (
            "pending"
            if row.status == "running"
            else "failed"
            if row.status == "superseded"
            else row.status
        )
        replacement = target.model_dump() | {
            "readiness": readiness,
            "error_code": row.error_code,
            "attempt_id": row.id,
            "attempt_number": row.number,
            "retryable": row.retryable,
        }
        data.targets[index] = SkillOperationTarget.model_validate(replacement)
    states = {row.status for row in current.values()}
    if operation.replacement_id is not None or "superseded" in states:
        status = "superseded"
    elif states & {"pending", "running"}:
        status = "preparing"
    elif "needs_resolution" in states:
        status = "needs_resolution"
    elif states & {"failed", "unsupported"}:
        status = "failed"
    elif states <= {"stored"}:
        status = "stored"
    else:
        status = "ready"
    retryable = status == "failed" and any(row.retryable for row in current.values())
    return data, status, retryable


def save_projection(operation: SkillOperation, current: dict[UUID, SkillDeploymentAttempt]) -> None:
    """
    在持有用户锁的状态事务内同步投影，保活检查据此观察同一阶段。

    :param operation (SkillOperation): 原始操作
    :param current (dict[UUID, SkillDeploymentAttempt]): 已更新的完整目标当前尝试
    """
    data, operation.status, operation.retryable = attempt_projection(operation, current)
    operation.result_json = data.model_dump(mode="json")
