"""
验证真实配置受理按有效目标取代旧操作，保留原始尝试和仍活动的内容引用。
"""

from uuid import UUID, uuid4

import pytest
from deployment_attempt_support import bound_account, legacy_attempts, observe
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_content_service import database as database
from test_skill_deployment_attempts import failed, history, retry
from test_skill_deployment_plans import inspect, plans
from test_skill_library import LibraryHarness
from test_skill_library import library as library

from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.schemas.skill_library import (
    SkillRuleRequest,
    SkillScope,
    SkillUpdateRequest,
)
from agent_remote_server.schemas.skill_results import SkillMutationData, SkillResult
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.clocks import retention_mutation


async def status(library: LibraryHarness, identity: UUID) -> SkillResult[SkillMutationData]:
    """
    在独立事务读取原操作，避免把后来受理替换成原结果。

    :param library (LibraryHarness): 原用户库
    :param identity (UUID): 原始操作身份
    :return SkillResult[SkillMutationData]: 已校验的原操作当前观察
    """
    async with library.database() as session:
        return await library.service(session).status(library.owner, identity)


async def disable(
    library: LibraryHarness, account: UUID | None = None
) -> SkillResult[SkillMutationData]:
    """
    通过普通库变更触发自动替代，不直接设置操作状态。

    :param library (LibraryHarness): 原用户库
    :param account (UUID | None): 精确账户或默认范围
    :return SkillResult[SkillMutationData]: 新受理结果
    """
    return await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="learning",
            scope=SkillScope(account_id=account),
            idempotency_key=str(uuid4()),
            expected_generation=await library.generation(),
        )
    )


async def test_changed_failed_target_is_superseded_without_rewriting_history(
    library: LibraryHarness,
) -> None:
    """
    新配置原子标记旧失败操作，尝试历史和来源摘要完全不变。

    :param library (LibraryHarness): 原用户库
    """
    original, account, request = await failed(library)
    before = await history(library, original)
    original_plans = await plans(library, original)
    replacement = await disable(library, account)
    old = await status(library, original)
    assert old.status == "superseded" and not old.retryable and old.committed
    assert old.operation_id == original and old.data.replacement_id == replacement.operation_id
    assert old.data.generation == request.expected_generation
    after = await history(library, original)
    assert [(row.id, row.status, row.error_code, row.retryable) for row in after] == [
        (row.id, row.status, row.error_code, row.retryable) for row in before
    ]
    assert await plans(library, original) == original_plans
    with pytest.raises(SkillContentError) as error:
        await retry(library, original, request)
    assert error.value.code == "OPERATION_SUPERSEDED"
    assert not (await inspect(library)).reasons("operation", original)


@pytest.mark.parametrize("phase", ["pending", "running", "needs_resolution"])
async def test_superseded_parent_keeps_active_targets_and_cannot_be_revived(
    library: LibraryHarness, phase: str
) -> None:
    """
    替代不是进程排空证据；最后目标结束前保活，迟到成功也不能复活父操作。

    :param library (LibraryHarness): 原用户库
    :param phase (str): 被取代时原目标实际阶段
    """
    original, account, request = await failed(library)
    await retry(library, original, request)
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        operation = await session.get(SkillOperation, original)
        assert operation is not None
        await observe(
            session,
            operation,
            phase,
            error_code="STATE_MIGRATION_REQUIRED" if phase == "needs_resolution" else None,
        )
    newer = await disable(library, account)
    old = await status(library, original)
    assert old.status == "superseded" and old.data.replacement_id == newer.operation_id
    assert (await inspect(library)).reasons("operation", original) == {"pending_operation"}
    assert (await retry(library, original, request)).id == original
    assert len(await history(library, original)) == 2
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        operation = await session.get(SkillOperation, original)
        assert operation is not None
        await observe(session, operation, "ready")
    finished = await status(library, original)
    assert finished.status == "superseded" and finished.data.targets[0].readiness == "ready"
    assert finished.data.replacement_id == newer.operation_id
    assert not (await inspect(library)).reasons("operation", original)


async def test_first_replacement_is_stable_across_later_changes_and_replay(
    library: LibraryHarness,
) -> None:
    """
    原操作永远指向第一次实际替代，不把最新配置冒充最初的替代原因。

    :param library (LibraryHarness): 原用户库
    """
    original, account, _ = await failed(library)
    second = await disable(library, account)
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        row = await session.get(SkillOperation, second.operation_id)
        assert row is not None
        await observe(session, row, "failed", retryable=True, error_code="TRANSFER_FAILED")
    third = await library.execute(
        SkillRuleRequest(
            command="enable",
            skill="learning",
            scope=SkillScope(account_id=account),
            idempotency_key="enable-again",
            expected_generation=await library.generation(),
        )
    )
    assert (await status(library, original)).data.replacement_id == second.operation_id
    assert second.operation_id is not None
    assert (await status(library, second.operation_id)).data.replacement_id == third.operation_id
    async with library.database() as session:
        row = await session.get(SkillOperation, original)
        assert row is not None
        assert (
            await library.service(session).status_by_key(library.owner, row.idempotency_key)
        ).status == "superseded"


@pytest.mark.parametrize(
    "change", ["other_account", "other_tool", "stage", "no_op", "unchanged_pin"]
)
async def test_unrelated_changes_do_not_replace_original_plan(
    library: LibraryHarness, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    不以代数变化推断原计划过期，只检查目标有效内容和真实绑定。

    :param library (LibraryHarness): 原用户库
    :param change (str): 不改变原目标选择的配置事件
    :param monkeypatch (pytest.MonkeyPatch): 临时工具适配器注册
    """
    original, account, _ = await failed(library)
    other = await bound_account(library)
    if change == "other_account":
        await disable(library, other)
    elif change == "other_tool":
        from agent_remote_server.services.tool_registry import ToolRegistry, ToolRuntimeTemplate

        monkeypatch.setitem(
            ToolRegistry._templates,
            "codex",
            ToolRuntimeTemplate(
                tool_type="codex",
                sandbox_agent="codex",
                command=["codex"],
                verifier="codex",
                account_config_subdir="codex",
            ),
        )
        await library.execute(
            SkillRuleRequest(
                command="disable",
                skill="learning",
                scope=SkillScope(tools=("codex",)),
                idempotency_key="other-tool",
                expected_generation=1,
            )
        )
    elif change == "stage":
        await library.execute(
            SkillUpdateRequest(
                skill="learning",
                item=await library.candidate(version="two"),
                stage=True,
                idempotency_key="stage",
                expected_generation=1,
            )
        )
    elif change == "no_op":
        await library.execute(
            SkillRuleRequest(
                command="enable",
                skill="learning",
                idempotency_key="already-enabled",
                expected_generation=1,
            )
        )
    else:
        first = (await library.info()).default_revision_id
        pinned = await library.execute(
            SkillRuleRequest(
                command="pin",
                skill="learning",
                revision=str(first),
                scope=SkillScope(account_id=account),
                idempotency_key="pin",
                expected_generation=1,
            )
        )
        assert pinned.operation_id is not None
        async with library.database.begin() as session, retention_mutation(session, library.owner):
            row = await session.get(SkillOperation, pinned.operation_id)
            assert row is not None
            await observe(session, row, "failed", retryable=True, error_code="TRANSFER_FAILED")
        await library.execute(
            SkillUpdateRequest(
                skill="learning",
                item=await library.candidate(version="two"),
                idempotency_key="update",
                expected_generation=2,
            )
        )
        # 原操作的另一个失败目标会变动；只检查账户范围 pin 操作本身。
        original = pinned.operation_id
    observed = await status(library, original)
    assert observed.status == "failed" and observed.data.replacement_id is None


@pytest.mark.parametrize("state", ["ready", "stored", "legacy"])
async def test_completed_or_unknown_history_is_not_inferred_superseded(
    library: LibraryHarness, state: str
) -> None:
    """
    完成的原目标及未知历史没有新的可推断部署义务。

    :param library (LibraryHarness): 原用户库
    :param state (str): 完成状态或历史缺失
    """
    original, _, _ = await failed(library)
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        row = await session.get(SkillOperation, original)
        assert row is not None
        if state == "legacy":
            await legacy_attempts(session, row)
        else:
            await observe(session, row, state)
    await disable(library)
    result = await status(library, original)
    assert result.status == ("failed" if state == "legacy" else state)
    assert result.data.replacement_id is None


async def test_supersession_rolls_back_with_new_configuration(
    library: LibraryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    新受理最终刷新后失败，旧替代标记、代数和新计划一起回滚。

    :param library (LibraryHarness): 原用户库
    :param monkeypatch (pytest.MonkeyPatch): 实际刷新后的故障注入
    """
    from agent_remote_server.services.skills import library as module

    original, account, _ = await failed(library)
    original_hook = module.supersede_deployments

    async def fail_after_supersession(session: AsyncSession, operation: SkillOperation) -> None:
        """
        运行真实替代及刷新，再注入异常以验证数据库保存点。

        :param session (AsyncSession): 真实配置事务
        :param operation (SkillOperation): 已追加的新操作
        """
        await original_hook(session, operation)
        await session.flush()
        raise RuntimeError("injected after supersession")

    with monkeypatch.context() as patch:
        patch.setattr(module, "supersede_deployments", fail_after_supersession)
        with pytest.raises(RuntimeError, match="injected"):
            await disable(library, account)
    assert await library.generation() == 1
    old = await status(library, original)
    assert old.status == "failed" and old.retryable and old.data.replacement_id is None
    assert (await inspect(library)).reasons("operation", original) == {"pending_operation"}
    assert (await disable(library, account)).committed
