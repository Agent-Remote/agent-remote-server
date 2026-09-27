"""
验证替代关系的归属、计划证据及真实 PostgreSQL 重试交错。
"""

import asyncio
from uuid import uuid4

import pytest
from deployment_attempt_support import bound_account, observe
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_deployment_attempts import failed, history, retry
from test_skill_deployment_plans import inspect
from test_skill_deployment_supersession import disable, status
from test_skill_library import LibraryHarness
from test_skill_library import library as library

from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.schemas.skill_library import SkillRuleRequest, SkillScope
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.clocks import retention_mutation


@pytest.mark.parametrize("state", ["ready", "unsupported", "failed"])
async def test_changed_terminal_nonretryable_target_does_not_replace_other_failure(
    library: LibraryHarness, state: str
) -> None:
    """
    已结束且不可重试的目标变化，不使另一个仍适用的失败目标失去恢复入口。

    :param library (LibraryHarness): 原用户库
    :param state (str): 另一个目标的真实终态
    """
    other = await bound_account(library)
    original, account, request = await failed(library)
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        operation = await session.get(SkillOperation, original)
        assert operation is not None
        await observe(
            session,
            operation,
            state,
            account_id=other,
            error_code=None
            if state == "ready"
            else "SKILL_MANAGER_UNSUPPORTED"
            if state == "unsupported"
            else "AUTHORIZATION_DENIED",
        )
    await disable(library, other)
    before = await status(library, original)
    assert before.status == "failed" and before.retryable and before.data.replacement_id is None
    assert (await retry(library, original, request)).status == "preparing"
    assert [
        row.number for row in await history(library, original) if row.account_id == account
    ] == [1, 2]
    assert [row.number for row in await history(library, original) if row.account_id == other] == [
        1
    ]


@pytest.mark.parametrize("corruption", ["missing", "self", "foreign", "unrelated", "unchanged"])
async def test_invalid_replacement_blocks_status_and_retention(
    library: LibraryHarness, corruption: str
) -> None:
    """
    替代身份必须有同用户较新受理及共同目标的已保存选择变化证据。

    :param library (LibraryHarness): 原用户库
    :param corruption (str): 独立破坏的替代关系
    """
    original, account, _ = await failed(library)
    newer = await disable(library, account)
    identity = newer.operation_id
    if corruption == "missing":
        identity = uuid4()
    elif corruption == "self":
        identity = original
    elif corruption == "foreign":
        foreign = LibraryHarness(library.database, library.root, await user(library.database))
        identity, _, _ = await failed(foreign)
    elif corruption == "unrelated":
        other = await bound_account(library)
        identity = (await disable(library, other)).operation_id
    else:
        identity = (await disable(library, account)).operation_id
    async with library.database.begin() as session:
        operation = await session.get(SkillOperation, original)
        assert operation is not None and identity is not None
        operation.replacement_id = identity
        operation.result_json = operation.result_json | {"replacement_id": str(identity)}
    with pytest.raises(ValueError, match="replacement"):
        await status(library, original)
    with pytest.raises(ValueError, match="replacement"):
        await inspect(library)


async def test_foreign_configuration_never_supersedes_same_named_user_skill(
    library: LibraryHarness,
) -> None:
    """
    相同技能名和代数不形成跨用户的部署关系。

    :param library (LibraryHarness): 原用户库
    """
    original, _, _ = await failed(library)
    foreign = LibraryHarness(library.database, library.root, await user(library.database))
    await failed(foreign)
    await disable(foreign)
    assert (await status(library, original)).status == "failed"
    assert (await status(library, original)).data.replacement_id is None


async def test_postgres_retry_and_replacement_serialize_without_lost_history(
    library: LibraryHarness,
) -> None:
    """
    独立连接的重试和新配置争用同一用户锁，不能提交无后继记录或复活旧计划。

    :param library (LibraryHarness): PostgreSQL 原用户库
    """
    if library.database.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires independent PostgreSQL row locks")
    original, account, request = await failed(library)
    mutation = SkillRuleRequest(
        command="disable",
        skill="learning",
        scope=SkillScope(account_id=account),
        idempotency_key="concurrent-disable",
        expected_generation=1,
    )
    changed, retried = await asyncio.gather(
        library.execute(mutation), retry(library, original, request), return_exceptions=True
    )
    assert not isinstance(changed, BaseException)
    if isinstance(retried, BaseException):
        assert isinstance(retried, SkillContentError) and retried.code == "OPERATION_SUPERSEDED"
    view = await status(library, original)
    assert view.status == "superseded" and not view.retryable
    assert view.data.replacement_id == changed.operation_id
    rows = await history(library, original)
    assert len(rows) == (1 if isinstance(retried, BaseException) else 2)
    assert rows[0].status == "failed" and rows[0].retryable
    async with library.database() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(SkillDeploymentAttempt)
            .where(
                SkillDeploymentAttempt.user_id == library.owner,
                SkillDeploymentAttempt.operation_id == original,
            )
        ) == len(rows)
    protected = await inspect(library)
    assert bool(protected.reasons("operation", original)) == (len(rows) == 2)
