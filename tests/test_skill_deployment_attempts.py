"""
验证重试追加原目标的精确后继，保留成功项并原子拒绝不适用计划。
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from deployment_attempt_support import bound_account, observe
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_deployment_plans import inspect, plans
from test_skill_library import LibraryHarness
from test_skill_library import library as library

from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_deployment_attempts import (
    SkillDeploymentAttempt,
    SkillDeploymentRetry,
)
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.schemas.skill_deployment_retry import (
    SkillDeploymentRetryRequest,
    SkillRetryTarget,
)
from agent_remote_server.schemas.skill_library import SkillRuleRequest, SkillScope
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.deployment_retry import retry_deployment
from agent_remote_server.services.skills.retention.clocks import retention_mutation


async def failed(library: LibraryHarness) -> tuple[UUID, UUID, SkillDeploymentRetryRequest]:
    """
    构造保留真实来源计划的失败观察，未运行任何模拟节点执行器。

    :param library (LibraryHarness): 原始用户库
    :return tuple[UUID, UUID, SkillDeploymentRetryRequest]: 原操作、原账户和精确请求
    """
    account = await bound_account(library)
    accepted = await library.add(await library.candidate())
    assert accepted.operation_id is not None
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        operation = await session.get(SkillOperation, accepted.operation_id)
        assert operation is not None
        attempts = await observe(
            session, operation, "failed", retryable=True, error_code="TRANSFER_FAILED"
        )
        request = SkillDeploymentRetryRequest(
            idempotency_key=str(uuid4()),
            expected_generation=accepted.data.generation,
            targets=(SkillRetryTarget(account_id=account, attempt_id=attempts[account].id),),
        )
    return accepted.operation_id, account, request


async def retry(
    library: LibraryHarness,
    operation_id: UUID,
    request: SkillDeploymentRetryRequest,
    owner: UUID | None = None,
) -> SkillOperation:
    """
    用独立提交事务模拟重试请求和响应丢失后的恢复。

    :param library (LibraryHarness): 用户库入口
    :param operation_id (UUID): 原始操作
    :param request (SkillDeploymentRetryRequest): 精确受理输入
    :param owner (UUID | None): 可选外来用户身份
    :return SkillOperation: 原操作当前投影
    """
    async with library.database.begin() as session:
        return await retry_deployment(session, owner or library.owner, operation_id, request)


async def history(
    library: LibraryHarness, operation_id: UUID
) -> tuple[SkillDeploymentAttempt, ...]:
    """
    在重启式独立事务中读取原始尝试历史。

    :param library (LibraryHarness): 原用户库
    :param operation_id (UUID): 原操作
    :return tuple[SkillDeploymentAttempt, ...]: 全部原始尝试
    """
    async with library.database() as session:
        return await SkillDeploymentAttemptRepository(session).attempts(library.owner, operation_id)


async def test_acceptance_records_original_terminal_attempts(library: LibraryHarness) -> None:
    """
    未绑定和不支持目标有独立观察，均不能借由记录存在宣称运行就绪。

    :param library (LibraryHarness): 原始用户库
    """
    await library.account()
    await bound_account(library)
    accepted = await library.add(await library.candidate())
    assert accepted.operation_id is not None
    attempts = await history(library, accepted.operation_id)
    assert {attempt.status for attempt in attempts} == {"stored", "unsupported"}
    assert all(attempt.number == 1 and attempt.predecessor_id is None for attempt in attempts)
    assert all(
        target.attempt_id is not None and target.attempt_number == 1
        for target in accepted.data.targets
    )
    async with library.database() as session:
        assert (
            await library.service(session).status(library.owner, accepted.operation_id) == accepted
        )


async def test_exact_retry_appends_once_and_preserves_successful_targets(
    library: LibraryHarness,
) -> None:
    """
    部分成功不能重复排队，重试键在后续成功后仍只恢复原受理。

    :param library (LibraryHarness): 原始用户库
    """
    successful = await bound_account(library)
    operation_id, account, request = await failed(library)
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        await observe(session, operation, "ready", account_id=successful)
    async with library.database() as session:
        view = await library.service(session).status(library.owner, operation_id)
        selected = next(target for target in view.data.targets if target.account_id == account)
        assert selected.retryable and selected.attempt_id == request.targets[0].attempt_id
        assert view.errors[0].message == "deployment transfer failed; original input is retained"
    original = await plans(library, operation_id)
    first = await retry(library, operation_id, request)
    assert first.status == "preparing" and not first.retryable
    rows = await history(library, operation_id)
    assert len(rows) == 3
    saved_success = next(row for row in rows if row.account_id == successful)
    assert saved_success.status == "ready" and saved_success.number == 1
    successor = next(row for row in rows if row.number == 2)
    assert (
        successor.account_id == account
        and successor.predecessor_id == request.targets[0].attempt_id
    )
    assert successor.status == "pending"
    assert (await retry(library, operation_id, request)).id == operation_id
    assert len(await history(library, operation_id)) == 3
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        await observe(session, operation, "ready", account_id=account)
    assert (await retry(library, operation_id, request)).status == "ready"
    assert len(await history(library, operation_id)) == 3
    assert await plans(library, operation_id) == original
    assert await library.generation() == request.expected_generation


@pytest.mark.parametrize(
    "change",
    [
        "key",
        "attempt",
        "generation",
        "binding",
        "selection",
        "account",
        "conflict",
        "permission",
        "superseded",
    ],
)
async def test_retry_rejects_changed_authority_without_appending(
    library: LibraryHarness, change: str
) -> None:
    """
    原计划、前序和权限任何变化都不能通过重试转成新的执行授权。

    :param library (LibraryHarness): 原用户库
    :param change (str): 要破坏的独立授权边界
    """
    operation_id, account_id, request = await failed(library)
    expected = {
        "key": "IDEMPOTENCY_CONFLICT",
        "attempt": "ATTEMPT_CHANGED",
        "generation": "GENERATION_CONFLICT",
        "binding": "DEPLOYMENT_BINDING_CHANGED",
        "selection": "OPERATION_SUPERSEDED",
        "account": "ACCOUNT_NOT_AVAILABLE",
        "conflict": "OPERATION_NOT_RETRYABLE",
        "permission": "OPERATION_NOT_RETRYABLE",
        "superseded": "OPERATION_SUPERSEDED",
    }[change]
    if change == "key":
        await retry(library, operation_id, request)
        request = request.model_copy(update={"expected_generation": 999})
    elif change == "attempt":
        request = request.model_copy(
            update={"targets": (SkillRetryTarget(account_id=account_id, attempt_id=uuid4()),)}
        )
    elif change == "generation":
        request = request.model_copy(update={"expected_generation": 999})
    elif change == "selection":
        await library.execute(
            SkillRuleRequest(
                command="disable",
                skill="learning",
                scope=SkillScope(account_id=account_id),
                idempotency_key=str(uuid4()),
                expected_generation=1,
            )
        )
    else:
        async with library.database.begin() as session, retention_mutation(session, library.owner):
            account = await session.get(ToolAccount, account_id)
            operation = await session.get(SkillOperation, operation_id)
            assert account is not None and operation is not None
            if change == "binding":
                account.runtime_backend = "docker_sandbox"
            elif change == "account":
                account.status = "disabled"
            elif change == "conflict":
                await observe(
                    session, operation, "needs_resolution", error_code="STATE_MIGRATION_REQUIRED"
                )
            elif change == "permission":
                await observe(session, operation, "failed", error_code="AUTHORIZATION_DENIED")
            else:
                await observe(session, operation, "superseded", error_code="OPERATION_SUPERSEDED")
    before = await history(library, operation_id)
    with pytest.raises(SkillContentError) as error:
        await retry(library, operation_id, request)
    assert error.value.code == expected
    assert [row.id for row in await history(library, operation_id)] == [row.id for row in before]


async def test_selection_is_all_or_nothing_and_unrelated_generation_is_allowed(
    library: LibraryHarness,
) -> None:
    """
    任一不可重试目标使整批拒绝，其他账户规则变化不改写原账户计划。

    :param library (LibraryHarness): 原用户库
    """
    other = await bound_account(library)
    operation_id, account, request = await failed(library)
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        attempts = await observe(
            session,
            operation,
            "unsupported",
            account_id=other,
            error_code="SKILL_MANAGER_UNSUPPORTED",
        )
        other_target = SkillRetryTarget(account_id=other, attempt_id=attempts[other].id)
    invalid = request.model_copy(update={"targets": (*request.targets, other_target)})
    with pytest.raises(SkillContentError, match="target cannot be retried"):
        await retry(library, operation_id, invalid)
    assert len(await history(library, operation_id)) == 2
    async with library.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillDeploymentRetry)
                .where(SkillDeploymentRetry.user_id == library.owner)
            )
            == 0
        )
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="learning",
            scope=SkillScope(account_id=other),
            idempotency_key=str(uuid4()),
            expected_generation=1,
        )
    )
    assert await library.generation() == 2
    assert (await retry(library, operation_id, request)).status == "preparing"
    assert len(await history(library, operation_id)) == 3
    assert account != other


async def test_pending_retry_retains_original_plan_and_rejects_cross_user(
    library: LibraryHarness,
) -> None:
    """
    待执行重试仍保活精确来源，知道操作与摘要不能越过用户归属。

    :param library (LibraryHarness): 原用户库
    """
    operation_id, _, request = await failed(library)
    original = await plans(library, operation_id)
    await retry(library, operation_id, request)
    retained = await inspect(library)
    assert "pending_operation" in retained.reasons("revision", original[0].sources[0].revision_id)
    with pytest.raises(SkillContentError) as error:
        await retry(library, operation_id, request, uuid4())
    assert error.value.code == "OPERATION_NOT_FOUND"


async def test_postgres_concurrent_recovery_reuses_one_successor(library: LibraryHarness) -> None:
    """
    两个独立请求同时恢复相同重试键时只提交一个后继。

    :param library (LibraryHarness): 生产行锁测试入口
    """
    async with library.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires PostgreSQL row locks")
    operation_id, _, request = await failed(library)
    first, second = await asyncio.gather(
        retry(library, operation_id, request), retry(library, operation_id, request)
    )
    assert first.id == second.id == operation_id
    assert len(await history(library, operation_id)) == 2


async def test_status_refreshes_previously_loaded_operation(library: LibraryHarness) -> None:
    """
    复用会话中的旧 ORM 对象不能混合新尝试和旧投影，查询本身不创建尝试。

    :param library (LibraryHarness): 原用户库
    """
    operation_id, _, request = await failed(library)
    async with library.database() as session:
        cached = await session.get(SkillOperation, operation_id)
        assert cached is not None and cached.status == "failed"
        await session.commit()
        await retry(library, operation_id, request)
        assert cached.status == "failed"
        result = await library.service(session).status(library.owner, operation_id)
        assert result.status == "preparing" and cached.status == "preparing"
        assert result.data.targets[0].attempt_number == 2
    assert len(await history(library, operation_id)) == 2
