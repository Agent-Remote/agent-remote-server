"""
验证尝试链数据库隔离、完整性检查和失败重试事务的原子回滚。
"""

from uuid import uuid4

import pytest
from deployment_attempt_support import bound_account, observe
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_content_service import database as database
from test_skill_deployment_attempts import failed, history, retry
from test_skill_deployment_plans import inspect
from test_skill_library import LibraryHarness
from test_skill_library import library as library

from agent_remote_server.models.skill_deployment_attempts import (
    SkillDeploymentAttempt,
    SkillDeploymentRetry,
)
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.clocks import retention_mutation


@pytest.mark.parametrize(
    "violation", ["owner", "predecessor", "permission", "duplicate", "self", "state_error"]
)
async def test_database_rejects_foreign_or_unsafe_attempts(
    library: LibraryHarness, violation: str
) -> None:
    """
    用户、账户、前序和可重试错误约束在数据库中独立生效。

    :param library (LibraryHarness): 原用户库
    :param violation (str): 待违反的独立持久化约束
    """
    other = await bound_account(library)
    operation_id, account, request = await failed(library)
    rows = await history(library, operation_id)
    original = next(row for row in rows if row.account_id == account)
    foreign = next(row for row in rows if row.account_id == other)
    identity = uuid4()
    candidate = SkillDeploymentAttempt(
        id=identity,
        user_id=uuid4() if violation == "owner" else library.owner,
        operation_id=operation_id,
        account_id=account,
        number=1 if violation == "duplicate" else 2,
        predecessor_id=None
        if violation == "duplicate"
        else foreign.id
        if violation == "predecessor"
        else identity
        if violation == "self"
        else request.targets[0].attempt_id,
        plan_digest=original.plan_digest,
        status="failed" if violation == "permission" else "pending",
        retryable=violation == "permission",
        error_code="AUTHORIZATION_DENIED" if violation in {"permission", "state_error"} else None,
    )
    with pytest.raises(IntegrityError):
        async with library.database.begin() as session:
            session.add(candidate)
            await session.flush()
    assert len(await history(library, operation_id)) == 2


@pytest.mark.parametrize("corruption", ["missing", "projection", "digest", "gap", "version"])
async def test_corruption_blocks_status_and_retention(
    library: LibraryHarness, corruption: str
) -> None:
    """
    缺失或漂移的原始尝试不能被解释为零部署义务或可回收内容。

    :param library (LibraryHarness): 原用户库
    :param corruption (str): 注入的持久化损坏
    """
    operation_id, account, _ = await failed(library)
    async with library.database.begin() as session:
        operation = await session.get(SkillOperation, operation_id)
        attempt = await session.scalar(
            select(SkillDeploymentAttempt).where(
                SkillDeploymentAttempt.operation_id == operation_id
            )
        )
        assert operation is not None and attempt is not None
        if corruption == "missing":
            await session.delete(attempt)
        elif corruption == "projection":
            operation.retryable = False
        elif corruption == "digest":
            attempt.plan_digest = "f" * 64
        elif corruption == "version":
            operation.attempts_version = 999
        else:
            session.add(
                SkillDeploymentAttempt(
                    id=uuid4(),
                    user_id=library.owner,
                    operation_id=operation_id,
                    account_id=account,
                    number=3,
                    predecessor_id=attempt.id,
                    plan_digest=attempt.plan_digest,
                    status="pending",
                    retryable=False,
                )
            )
    with pytest.raises(ValueError):
        await inspect(library)
    async with library.database() as session:
        with pytest.raises(ValueError):
            await library.service(session).status(library.owner, operation_id)


async def test_failure_after_successor_flush_rolls_back_complete_retry(
    library: LibraryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    后继和重试收据已经 flush 后失败仍回滚状态投影及全部新增记录。

    :param library (LibraryHarness): 原用户库
    :param monkeypatch (pytest.MonkeyPatch): 注入已写入数据库后的异常
    """
    operation_id, _, request = await failed(library)
    original_flush = AsyncSession.flush

    async def fail_after_flush(self: AsyncSession, objects: object = None) -> None:
        """
        在真实追加写入之后触发异常，不能只测试尚未保存的内存对象。

        :param objects (object): SQLAlchemy 可选刷新集合，本场景不使用
        """
        assert objects is None
        inserted = any(isinstance(row, SkillDeploymentRetry) for row in self.new)
        await original_flush(self)
        if inserted:
            raise RuntimeError("injected after retry flush")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "flush", fail_after_flush)
        with pytest.raises(RuntimeError, match="injected"):
            await retry(library, operation_id, request)
    assert len(await history(library, operation_id)) == 1
    async with library.database() as session:
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None and operation.status == "failed" and operation.retryable
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillDeploymentRetry)
                .where(SkillDeploymentRetry.user_id == library.owner)
            )
            == 0
        )
    assert (await retry(library, operation_id, request)).status == "preparing"


async def test_initial_attempt_failure_rolls_back_configuration(
    library: LibraryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    首批尝试已写入后出现异常也回滚配置、计划和全部受理对象。

    :param library (LibraryHarness): 原用户库
    :param monkeypatch (pytest.MonkeyPatch): 初始尝试保存后的故障注入
    """
    from agent_remote_server.models.skill_deployment import (
        SkillDeploymentEntry,
        SkillDeploymentTarget,
    )
    from agent_remote_server.schemas.skill_results import SkillOperationTarget
    from agent_remote_server.services.skills import library as library_service

    await bound_account(library)
    candidate = await library.candidate()
    original = library_service.initial_attempts

    async def fail_after_attempts(
        session: AsyncSession, operation: SkillOperation, targets: list[SkillOperationTarget]
    ) -> None:
        """
        保留真实追加流程，只在全部行已刷新后模拟失败。

        :param session (AsyncSession): 原始受理事务
        :param operation (SkillOperation): 未提交操作
        :param targets (list[SkillOperationTarget]): 已固定原目标
        """
        await original(session, operation, targets)
        raise RuntimeError("injected after initial attempts")

    monkeypatch.setattr(library_service, "initial_attempts", fail_after_attempts)
    with pytest.raises(RuntimeError, match="injected"):
        await library.add(candidate)
    assert await library.generation() == 0
    async with library.database() as session:
        for model in (
            SkillOperation,
            SkillDeploymentTarget,
            SkillDeploymentEntry,
            SkillDeploymentAttempt,
            SkillDeploymentRetry,
        ):
            assert (
                await session.scalar(
                    select(func.count()).select_from(model).where(model.user_id == library.owner)
                )
                == 0
            )


async def test_supersession_keeps_other_active_targets_rooted(library: LibraryHarness) -> None:
    """
    操作被取代不能释放仍在执行的另一个目标所需内容，退出后才解除该根。

    :param library (LibraryHarness): 原用户库
    """
    other = await bound_account(library)
    operation_id, account, request = await failed(library)
    await retry(library, operation_id, request)
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        await observe(
            session, operation, "superseded", account_id=other, error_code="OPERATION_SUPERSEDED"
        )
        assert operation.status == "superseded"
    retained = await inspect(library)
    assert retained.reasons("operation", operation_id) == {"pending_operation"}
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        await observe(
            session, operation, "superseded", account_id=account, error_code="OPERATION_SUPERSEDED"
        )
    assert not (await inspect(library)).reasons("operation", operation_id)


async def test_unknown_legacy_attempts_keep_retention_without_advertising_retry(
    library: LibraryHarness,
) -> None:
    """
    升级前未知尝试继续保留内容，但查询不能宣称它已具备新版重试授权。

    :param library (LibraryHarness): 原用户库
    """
    from deployment_attempt_support import legacy_attempts

    operation_id, _, request = await failed(library)
    async with library.database.begin() as session:
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        await legacy_attempts(session, operation)
    async with library.database() as session:
        view = await library.service(session).status(library.owner, operation_id)
        assert not view.retryable
        assert view.data.targets[0].attempt_id is None
    assert (await inspect(library)).reasons("operation", operation_id) == {"pending_operation"}
    with pytest.raises(SkillContentError) as error:
        await retry(library, operation_id, request)
    assert error.value.code == "OPERATION_NOT_RETRYABLE"
