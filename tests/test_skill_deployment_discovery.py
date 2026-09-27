"""
验证首次接管发现只补充明确原来源，原受理和后来的账户配置保持独立。
"""

from uuid import UUID

import pytest
from deployment_attempt_support import observe
from skill_takeover_support import TakeoverHarness, tree
from skill_takeover_support import takeover as takeover
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_library import library as library

from agent_remote_server.models.skill_deployment_discovery import SkillDeploymentDiscovery
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.services.skills.deployment_discovery import execution_plans
from agent_remote_server.services.skills.deployment_validation import saved_plans


async def accepted(takeover: TakeoverHarness) -> UUID:
    """
    创建尚未接管账户的真实配置受理，只把执行进度置为内部待调度状态。

    :param takeover (TakeoverHarness): 原账户接管环境
    :return UUID: 原配置操作身份
    """
    result = await takeover.library.add(await takeover.library.candidate(name="installed"))
    assert result.operation_id is not None
    async with takeover.library.database.begin() as session:
        operation = await session.get(SkillOperation, result.operation_id)
        assert operation is not None
        await observe(session, operation, "pending")
    return result.operation_id


async def test_takeover_discovery_preserves_accepted_plan_and_fixes_execution_sources(
    takeover: TakeoverHarness,
) -> None:
    """
    原计划不包含未知手工条目，接管后独立解析完整来源且重复提交不改变摘要。

    :param takeover (TakeoverHarness): 原始未接管账户
    """
    operation_id = await accepted(takeover)
    async with takeover.library.database() as session:
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        original = saved_plans(
            operation,
            *await SkillDeploymentRepository(session).rows(operation.user_id, operation.id),
        )[0]
        boundary = await session.scalar(
            select(SkillDeploymentDiscovery).where(
                SkillDeploymentDiscovery.operation_id == operation_id
            )
        )
        assert boundary is not None and boundary.takeover_id is None
        assert boundary.original_digest == original.digest()
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    files = {"manual/SKILL.md": b"# Private manual skill\n", "manual/data.bin": b"\x00state"}
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree(files)))
    await takeover.transfer(receipt, files)
    receipt = await takeover.complete(receipt)
    async with takeover.library.database() as session:
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        plans = saved_plans(
            operation,
            *await SkillDeploymentRepository(session).rows(operation.user_id, operation.id),
        )
        assert plans[0] == original
        resolved = (await execution_plans(session, plans))[0]
        assert {source.name for source in resolved.sources} == {"installed", "manual"}
        assert resolved.digest() != original.digest()
        boundary = await session.scalar(
            select(SkillDeploymentDiscovery).where(
                SkillDeploymentDiscovery.operation_id == operation_id
            )
        )
        assert boundary is not None and boundary.takeover_id == receipt.id
        assert boundary.resolved_digest == resolved.digest()
    await takeover.complete(receipt)
    async with takeover.library.database() as session:
        assert (await execution_plans(session, (original,)))[0] == resolved


@pytest.mark.parametrize("files", [{}, {"root.txt": b"auxiliary"}])
async def test_empty_skill_discovery_still_seals_original_takeover(
    takeover: TakeoverHarness, files: dict[str, bytes]
) -> None:
    """
    没有新技能时也记录已解析回执，不能在将来追加别的来源。

    :param takeover (TakeoverHarness): 原始未接管账户
    :param files (dict[str, bytes]): 无技能的原目录内容
    """
    operation_id = await accepted(takeover)
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree(files)))
    await takeover.transfer(receipt, files)
    receipt = await takeover.complete(receipt)
    async with takeover.library.database() as session:
        boundary = await session.scalar(
            select(SkillDeploymentDiscovery).where(
                SkillDeploymentDiscovery.operation_id == operation_id
            )
        )
        assert boundary is not None and boundary.takeover_id == receipt.id
        assert boundary.original_digest == boundary.resolved_digest


async def test_unsupported_terminal_acceptance_does_not_expand_on_later_takeover(
    takeover: TakeoverHarness,
) -> None:
    """
    历史不支持的终态受理不能批量复制后来来源，也不能因接管恢复执行资格。

    :param takeover (TakeoverHarness): 原始未接管账户
    """
    result = await takeover.library.add(await takeover.library.candidate(name="installed"))
    assert result.status == "failed" and result.data.targets[0].readiness == "unsupported"
    assert result.operation_id is not None
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    files = {"manual/SKILL.md": b"# Existing manual skill\n"}
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree(files)))
    await takeover.transfer(receipt, files)
    await takeover.complete(receipt)
    async with takeover.library.database() as session:
        boundary = await session.scalar(
            select(SkillDeploymentDiscovery).where(
                SkillDeploymentDiscovery.operation_id == result.operation_id
            )
        )
        assert boundary is not None and boundary.takeover_id is None
        observed = await takeover.library.service(session).status(
            takeover.library.owner, result.operation_id
        )
        assert observed.status == "failed" and observed.data.targets[0].readiness == "unsupported"
