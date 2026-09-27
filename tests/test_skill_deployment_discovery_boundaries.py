"""
验证发现解析的事务失败、元数据损坏与并发受理不会扩大原输入。
"""

import asyncio

import pytest
from skill_takeover_support import TakeoverHarness, tree
from skill_takeover_support import takeover as takeover
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_content_service import database as database
from test_skill_deployment_discovery import accepted
from test_skill_deployment_discovery_lifecycle import discovered
from test_skill_library import library as library

from agent_remote_server.models.skill_deployment_discovery import (
    SkillDeploymentDiscoveredSource,
    SkillDeploymentDiscovery,
)
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, SkillCheckpoint
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.services.skills import takeover as takeover_service
from agent_remote_server.services.skills.deployment_discovery import (
    execution_plans,
    resolve_takeover_discoveries,
)
from agent_remote_server.services.skills.deployment_validation import saved_plans


async def test_discovery_failure_rolls_back_whole_takeover_publication(
    takeover: TakeoverHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    补充来源保存后的故障不发布半个接管，重试仍从原捕获完成。

    :param takeover (TakeoverHarness): 原账户及持久内容
    :param monkeypatch (pytest.MonkeyPatch): 本次故障注入
    """
    operation_id = await accepted(takeover)
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    files = {"manual/SKILL.md": b"# Original instructions\n"}
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree(files)))
    await takeover.transfer(receipt, files)
    async with takeover.library.database() as session:
        before = await session.scalar(select(func.count()).select_from(SkillCheckpoint))

    async def interrupted(session: AsyncSession, original: SkillAccountTakeover) -> None:
        """
        在真实解析写入后中断，验证外层保存点回滚。

        :param session (AsyncSession): 当前接管保存点
        :param original (SkillAccountTakeover): 原上传绑定
        """
        await resolve_takeover_discoveries(session, original)
        raise RuntimeError("injected publication interruption")

    with monkeypatch.context() as patch:
        patch.setattr(takeover_service, "resolve_takeover_discoveries", interrupted)
        with pytest.raises(RuntimeError, match="injected publication"):
            await takeover.complete(receipt)
    async with takeover.library.database() as session:
        boundary = await session.scalar(
            select(SkillDeploymentDiscovery).where(
                SkillDeploymentDiscovery.operation_id == operation_id
            )
        )
        directory = await session.get(AccountSkillDirectoryState, takeover.account)
        assert (
            boundary is not None
            and boundary.takeover_id is None
            and boundary.resolved_digest is None
        )
        assert (
            directory is not None
            and directory.mode == "migrating"
            and directory.head_checkpoint_id is None
        )
        assert (
            await session.scalar(select(func.count()).select_from(SkillDeploymentDiscoveredSource))
            == 0
        )
        assert await session.scalar(select(func.count()).select_from(SkillCheckpoint)) == before
    receipt = await takeover.complete(receipt)
    assert receipt.status == "committed"


@pytest.mark.parametrize("change", ["missing_source", "digest", "epoch", "source_name"])
async def test_damaged_resolution_cannot_be_reconstructed_from_live_selection(
    takeover: TakeoverHarness, change: str
) -> None:
    """
    损坏证据必须报错，不能从后来账户状态重建看似相同的计划。

    :param takeover (TakeoverHarness): 原始未接管账户
    :param change (str): 要损坏的原解析字段
    """
    operation_id, _ = await discovered(takeover)
    async with takeover.library.database.begin() as session:
        boundary = await session.scalar(
            select(SkillDeploymentDiscovery).where(
                SkillDeploymentDiscovery.operation_id == operation_id
            )
        )
        source = await session.scalar(
            select(SkillDeploymentDiscoveredSource).where(
                SkillDeploymentDiscoveredSource.operation_id == operation_id
            )
        )
        assert boundary is not None and source is not None
        if change == "missing_source":
            await session.delete(source)
        elif change == "digest":
            boundary.resolved_digest = "0" * 64
        elif change == "epoch":
            boundary.directory_epoch += 1
        else:
            source.name = "changed"
    async with takeover.library.database() as session:
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        plans = saved_plans(
            operation,
            *await SkillDeploymentRepository(session).rows(operation.user_id, operation.id),
        )
        with pytest.raises(ValueError):
            await execution_plans(session, plans)


async def test_acceptance_waits_for_atomic_takeover_resolution(
    takeover: TakeoverHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    独立连接上的新受理必须等待接管解析提交，不能看到中间来源集合。

    :param takeover (TakeoverHarness): 原始未接管账户
    :param monkeypatch (pytest.MonkeyPatch): 发布事务阻塞点
    """
    async with takeover.library.database() as session:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL transaction locks")
    await accepted(takeover)
    candidate = await takeover.library.candidate(name="later")
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    files = {"manual/SKILL.md": b"# Original manual skill\n"}
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree(files)))
    await takeover.transfer(receipt, files)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(session: AsyncSession, original: SkillAccountTakeover) -> None:
        """
        在解析已写入但未提交时保留用户锁。

        :param session (AsyncSession): 原发布事务
        :param original (SkillAccountTakeover): 原接管身份
        """
        await resolve_takeover_discoveries(session, original)
        entered.set()
        await asyncio.wait_for(release.wait(), 5)

    with monkeypatch.context() as patch:
        patch.setattr(takeover_service, "resolve_takeover_discoveries", delayed)
        publication = asyncio.create_task(takeover.complete(receipt))
        await asyncio.wait_for(entered.wait(), 5)
        acceptance = asyncio.create_task(takeover.library.add(candidate))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(acceptance), 0.1)
        finally:
            release.set()
        await publication
        result = await acceptance
    assert result.operation_id is not None
    async with takeover.library.database() as session:
        assert (
            await session.scalar(
                select(SkillDeploymentDiscovery).where(
                    SkillDeploymentDiscovery.operation_id == result.operation_id
                )
            )
            is None
        )
        operation = await session.get(SkillOperation, result.operation_id)
        assert operation is not None
        plan = saved_plans(
            operation,
            *await SkillDeploymentRepository(session).rows(operation.user_id, operation.id),
        )[0]
        assert {source.name for source in plan.sources} == {"installed", "manual", "later"}


async def test_discovery_references_cannot_cross_accounts_or_owners(
    takeover: TakeoverHarness,
) -> None:
    """
    即使知道别的接管和本地版本身份，数据库归属外键及只读查询仍拒绝混用。

    :param takeover (TakeoverHarness): 当前用户原账户
    """
    from uuid import uuid4

    from sqlalchemy.exc import IntegrityError

    from agent_remote_server.models import ToolAccount
    from agent_remote_server.models.skill_local import AccountLocalSkill
    from agent_remote_server.repositories.skill_deployment_discovery import (
        SkillDeploymentDiscoveryRepository,
    )

    operation_id, receipt = await discovered(takeover)
    account_id = await takeover.library.account()
    async with takeover.library.database.begin() as session:
        account = await session.get(ToolAccount, account_id)
        assert account is not None
        account.affinity_node_id, account.runtime_backend = takeover.node, "native"
    other = TakeoverHarness(takeover.library, takeover.node, account_id, takeover.settings)
    other_receipt = await other.reserve(key="other-account")
    await other.lease(other_receipt)
    files = {"private/SKILL.md": b"# Other account source\n"}
    other_receipt = await other.begin(other_receipt, other.capture(other_receipt, tree(files)))
    await other.transfer(other_receipt, files)
    other_receipt = await other.complete(other_receipt)
    with pytest.raises(IntegrityError):
        async with takeover.library.database.begin() as session:
            boundary = await session.scalar(
                select(SkillDeploymentDiscovery).where(
                    SkillDeploymentDiscovery.operation_id == operation_id
                )
            )
            assert boundary is not None
            boundary.takeover_id = other_receipt.id
            await session.flush()
    with pytest.raises(IntegrityError):
        async with takeover.library.database.begin() as session:
            source = await session.scalar(
                select(SkillDeploymentDiscoveredSource).where(
                    SkillDeploymentDiscoveredSource.operation_id == operation_id
                )
            )
            foreign = await session.scalar(
                select(AccountLocalSkill).where(AccountLocalSkill.account_id == account_id)
            )
            assert source is not None and foreign is not None
            assert foreign.default_revision_id is not None
            source.source_id, source.revision_id = foreign.id, foreign.default_revision_id
            await session.flush()
    async with takeover.library.database() as session:
        forged = SkillDeploymentDiscovery(
            user_id=uuid4(),
            operation_id=operation_id,
            account_id=takeover.account,
            directory_epoch=receipt.directory_epoch,
            original_digest="0" * 64,
            takeover_id=receipt.id,
            resolved_digest="0" * 64,
        )
        assert await SkillDeploymentDiscoveryRepository(session).receipts((forged,)) == ()
