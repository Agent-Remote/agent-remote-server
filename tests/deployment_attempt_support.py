"""
构造已保存的目标观察，测试重试事务而不宣称 Node 执行验收。
"""

from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_library import LibraryHarness

from agent_remote_server.models import Node, ToolAccount
from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.schemas.skill_results import SkillMutationData
from agent_remote_server.services.skills.deployment_attempts import (
    current_attempts,
    save_projection,
)


async def bound_account(library: LibraryHarness) -> UUID:
    """
    绑定原账户到真实外键节点，未伪造运行时能力。

    :param library (LibraryHarness): 测试用户库
    :return UUID: 绑定原始节点的账户
    """
    identity = await library.account()
    async with library.database.begin() as session:
        node = Node(id=uuid4(), name="部署测试", status="healthy", region_code="global")
        session.add(node)
        await session.flush()
        account = await session.get(ToolAccount, identity)
        assert account is not None
        account.status = "active"
        account.runtime_backend = "native"
        account.affinity_node_id = node.id
    return identity


async def observe(
    session: AsyncSession,
    operation: SkillOperation,
    status: str,
    *,
    retryable: bool = False,
    error_code: str | None = None,
    account_id: UUID | None = None,
) -> dict[UUID, SkillDeploymentAttempt]:
    """
    模拟独立调度器已提交的观察，原始计划不受测试状态变更影响。

    :param session (AsyncSession): 测试状态事务
    :param operation (SkillOperation): 原始受理
    :param status (str): 当前目标阶段
    :param retryable (bool): 是否属于可重试终态
    :param error_code (str | None): 有界稳定错误码
    :param account_id (UUID | None): 单个目标或全部目标
    :return dict[UUID, SkillDeploymentAttempt]: 同步投影后的当前尝试
    """
    current = current_attempts(
        operation,
        await SkillDeploymentAttemptRepository(session).attempts(operation.user_id, operation.id),
    )
    for identity, attempt in current.items():
        if account_id is None or identity == account_id:
            attempt.status, attempt.retryable, attempt.error_code = status, retryable, error_code
    save_projection(operation, current)
    await session.flush()
    return current


async def legacy_attempts(session: AsyncSession, operation: SkillOperation) -> None:
    """
    明确构造升级前的历史操作，不能保留新版本行却假装没有尝试身份。

    :param session (AsyncSession): 测试数据事务
    :param operation (SkillOperation): 没有真实执行历史的初始操作
    """
    await session.execute(
        delete(SkillDeploymentAttempt).where(SkillDeploymentAttempt.operation_id == operation.id)
    )
    operation.attempts_version = None
    data = SkillMutationData.model_validate(operation.result_json)
    for target in data.targets:
        target.attempt_id, target.attempt_number = None, None
        target.retryable = False
    operation.result_json = data.model_dump(mode="json")
