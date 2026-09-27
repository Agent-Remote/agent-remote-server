"""
验证配置计划跨事务固定原目标、来源选择、安装纪元及保活依赖。
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from deployment_attempt_support import observe
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_library import LibraryHarness
from test_skill_library import library as library

from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_deployment import SkillDeploymentEntry, SkillDeploymentTarget
from agent_remote_server.models.skill_library import SkillOperation, SkillRevision
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_deployment import SkillDeploymentPlan
from agent_remote_server.schemas.skill_library import (
    SkillAddRequest,
    SkillRemoveRequest,
    SkillRuleRequest,
    SkillScope,
    SkillUpdateRequest,
)
from agent_remote_server.services.skills.deployment_validation import saved_plans
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.skill_manager.retention.graph import RetentionProtection


async def plans(library: LibraryHarness, identity: UUID) -> tuple[SkillDeploymentPlan, ...]:
    """
    使用新的数据库事务读取原计划，不复用受理时的对象。

    :param library (LibraryHarness): 用户库测试入口
    :param identity (UUID): 原操作身份
    :return tuple[SkillDeploymentPlan, ...]: 经过摘要核验的原始计划
    """
    async with library.database() as session:
        operation = await session.get(SkillOperation, identity)
        assert operation is not None
        targets, entries = await SkillDeploymentRepository(session).rows(library.owner, identity)
        return saved_plans(operation, targets, entries)


async def inspect(library: LibraryHarness) -> RetentionProtection:
    """
    从真实索引计算保活，不能以计划元数据存在代替内容引用验证。

    :param library (LibraryHarness): 用户库入口
    :return RetentionProtection: 完整保活闭包
    """
    async with library.database() as session:
        return protection(
            await SkillRetentionRepository(session).load(library.owner), datetime.now(UTC)
        )


async def test_original_targets_rules_and_epochs_survive_replay(library: LibraryHarness) -> None:
    """
    重放不纳入新账户或新安装纪元，原来源和原后端绑定保持不变。

    :param library (LibraryHarness): 原始所有者入口
    """
    account = await library.account()
    async with library.database.begin() as session:
        row = await session.get(ToolAccount, account)
        assert row is not None
        row.runtime_backend = "native"
    candidate = await library.candidate()
    request = SkillAddRequest(items=(candidate,), idempotency_key="original", expected_generation=0)
    accepted = await library.execute(request)
    assert accepted.operation_id is not None
    original = await plans(library, accepted.operation_id)
    assert len(original) == 1 and original[0].account_id == account
    assert original[0].runtime_backend == "native"
    assert original[0].sources[0].origin == "library"
    assert original[0].sources[0].installation_epoch == 1
    assert accepted.data.targets[0].plan_digest == original[0].digest()
    await library.account()
    await library.execute(
        SkillRemoveRequest(
            skill="learning",
            idempotency_key="remove",
            expected_generation=1,
        )
    )
    await library.add(candidate)
    async with library.database.begin() as session:
        row = await session.get(ToolAccount, account)
        assert row is not None
        row.runtime_backend = "docker_sandbox"
    assert (await library.info()).epoch == 2
    assert await library.execute(request) == accepted
    assert await plans(library, accepted.operation_id) == original
    async with library.database() as session:
        assert await SkillDeploymentRepository(session).rows(uuid4(), accepted.operation_id) == (
            (),
            (),
        )


async def test_retryable_terminal_plan_retains_account_pin_and_release_clock(
    library: LibraryHarness,
) -> None:
    """
    失败但可重试的完整计划保留账户 pin，取代时记录最后释放时钟。

    :param library (LibraryHarness): 原始所有者入口
    """
    account = await library.account()
    await library.add(await library.candidate())
    first = (await library.info()).default_revision_id
    await library.execute(
        SkillRuleRequest(
            command="pin",
            skill="learning",
            revision=str(first),
            scope=SkillScope(account_id=account),
            idempotency_key="pin",
            expected_generation=1,
        )
    )
    await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=await library.candidate(version="two"),
            idempotency_key="update",
            expected_generation=2,
        )
    )
    disabled = await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="learning",
            scope=SkillScope(account_id=account),
            idempotency_key="disable",
            expected_generation=3,
        )
    )
    assert disabled.operation_id is not None
    original = (await plans(library, disabled.operation_id))[0]
    assert not original.sources[0].enabled and original.sources[0].revision_id == first
    async with library.database.begin() as session, retention_mutation(session, library.owner):
        operation = await session.get(SkillOperation, disabled.operation_id)
        assert operation is not None
        await observe(session, operation, "failed", retryable=True, error_code="TRANSFER_FAILED")
    before = await inspect(library)
    assert "pending_operation" in before.reasons("revision", first)
    replacement = await library.execute(
        SkillRuleRequest(
            command="unpin",
            skill="learning",
            scope=SkillScope(account_id=account),
            idempotency_key="unpin",
            expected_generation=4,
        )
    )
    assert not (await inspect(library)).reasons("revision", first)
    async with library.database() as session:
        operation = await session.get(SkillOperation, disabled.operation_id)
        assert operation is not None and operation.status == "superseded"
        assert operation.replacement_id == replacement.operation_id
        revision = await session.get(SkillRevision, first)
        assert revision is not None and revision.retention_released_at is not None
    assert (await plans(library, disabled.operation_id))[0] == original


async def test_plan_capture_failure_rolls_back_configuration_and_all_targets(
    library: LibraryHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    第二个目标写入失败也不能留下第一份计划、受理记录或配置变更。

    :param library (LibraryHarness): 原始所有者入口
    :param monkeypatch (pytest.MonkeyPatch): 注入第二个目标持久化后的错误
    """
    await library.account()
    await library.account()
    candidate = await library.candidate()
    original_add = SkillDeploymentRepository.add
    calls = 0

    async def fail_second(self: SkillDeploymentRepository, plan: SkillDeploymentPlan) -> None:
        """
        在真实写入之后模拟保存失败，验证外层保存点覆盖全部行。

        :param plan (SkillDeploymentPlan): 当前目标配置
        """
        nonlocal calls
        await original_add(self, plan)
        calls += 1
        if calls == 2:
            raise RuntimeError("injected capture failure")

    monkeypatch.setattr(SkillDeploymentRepository, "add", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        await library.add(candidate)
    assert calls == 2 and await library.generation() == 0
    async with library.database() as session:
        for model in (SkillOperation, SkillDeploymentTarget, SkillDeploymentEntry):
            assert (
                await session.scalar(
                    select(func.count()).select_from(model).where(model.user_id == library.owner)
                )
                == 0
            )


@pytest.mark.parametrize("corruption", ["digest", "missing", "version", "binding"])
async def test_corrupt_plans_fail_status_and_retention(
    library: LibraryHarness,
    corruption: str,
) -> None:
    """
    即使已结束操作也核验计划完整性，不能将损坏输入解释为空引用。

    :param library (LibraryHarness): 原始所有者入口
    :param corruption (str): 要注入的持久化损坏类型
    """
    await library.account()
    accepted = await library.add(await library.candidate())
    assert accepted.operation_id is not None
    async with library.database.begin() as session:
        operation = await session.get(SkillOperation, accepted.operation_id)
        targets, entries = await SkillDeploymentRepository(session).rows(
            library.owner, accepted.operation_id
        )
        assert operation is not None
        if corruption == "digest":
            entries[0].enabled = not entries[0].enabled
        elif corruption == "missing":
            await session.delete(entries[0])
        elif corruption == "version":
            operation.plan_version = 999
        else:
            targets[0].node_id = uuid4()
    with pytest.raises(ValueError):
        await inspect(library)
    async with library.database() as session:
        with pytest.raises(ValueError):
            await library.service(session).status(library.owner, accepted.operation_id)
