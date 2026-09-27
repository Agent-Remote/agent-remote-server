"""
验证发现后的真实部署、原输入重试、后来规则变更与初始内容保留。
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from skill_takeover_support import TakeoverHarness, tree
from skill_takeover_support import takeover as takeover
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_deployment_discovery import accepted
from test_skill_library import library as library
from test_skill_session_admission import capability

from agent_remote_server.models import Node, NodeTask, ToolAccount
from agent_remote_server.models.skill_deployment_discovery import SkillDeploymentDiscovery
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_deployment_retry import (
    SkillDeploymentRetryRequest,
    SkillRetryTarget,
)
from agent_remote_server.schemas.skill_deployment_termination import (
    SkillDeploymentDrain,
    SkillDeploymentTerminatedResult,
    SkillDeploymentTerminationRequest,
)
from agent_remote_server.schemas.skill_library import SkillRuleRequest, SkillScope
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_attempts import current_attempts
from agent_remote_server.services.skills.deployment_content import NodeDeploymentContent
from agent_remote_server.services.skills.deployment_discovery import execution_plans
from agent_remote_server.services.skills.deployment_dispatch import SkillDeploymentDispatch
from agent_remote_server.services.skills.deployment_retry import retry_deployment
from agent_remote_server.services.skills.deployment_termination import NodeDeploymentTermination
from agent_remote_server.services.skills.deployment_validation import saved_plans
from agent_remote_server.services.skills.retention.build import protection


async def discovered(takeover: TakeoverHarness) -> tuple[UUID, SkillAccountTakeover]:
    """
    提交真实接管内容并保留一个原始待执行配置受理。

    :param takeover (TakeoverHarness): 原始未接管账户
    :return tuple[UUID, SkillAccountTakeover]: 原配置和完成的接管
    """
    operation_id = await accepted(takeover)
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    files = {"manual/SKILL.md": b"# Manual instructions\n", "root.txt": b"private root data"}
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree(files)))
    await takeover.transfer(receipt, files)
    receipt = await takeover.complete(receipt)
    async with takeover.library.database.begin() as session:
        account = await session.get(ToolAccount, takeover.account)
        assert account is not None
        account.status = "active"
        node = await session.get(Node, takeover.node)
        assert node is not None
        node.supported_tool_types = ["claude"]
        node.runtime_capabilities = {
            "backends": ["native"],
            "skill_manager": {"native": capability() | {"deployment_protocol_version": 1}},
        }
    return operation_id, receipt


async def reserve(takeover: TakeoverHarness, operation_id: UUID) -> SkillDeploymentTask:
    """
    原操作当前尝试通过生产预约入口创建或复用完整部署输入。

    :param takeover (TakeoverHarness): 已完成接管的原账户
    :param operation_id (UUID): 原始配置身份
    :return SkillDeploymentTask: 已提交任务及原完整输入
    """
    async with takeover.library.database.begin() as session:
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        current = current_attempts(
            operation,
            await SkillDeploymentAttemptRepository(session).attempts(
                operation.user_id, operation.id
            ),
        )
        binding = await SkillDeploymentDispatch(session, takeover.settings).reserve(
            operation.user_id, operation.id, takeover.account, current[takeover.account].id
        )
        assert isinstance(binding, SkillDeploymentTask)
        task = await session.get(NodeTask, binding.task_id)
        assert task is not None
        task.status, task.retry_count = "leased", 1
        task.lease_until = datetime.now(UTC) + timedelta(minutes=5)
        return binding


async def test_discovered_sources_enter_real_task_and_exact_retry(
    takeover: TakeoverHarness,
) -> None:
    """
    解析摘要用于实际清单和任务，失败排空后的重试复用同一完整输入。

    :param takeover (TakeoverHarness): 原始未接管账户
    """
    operation_id, receipt = await discovered(takeover)
    binding = await reserve(takeover, operation_id)
    async with takeover.library.database.begin() as session:
        content = await NodeDeploymentContent(session, takeover.settings).describe(
            takeover.node, binding.task_id, binding.attempt_id, 1
        )
        assert {member.entry_name for member in content.items} == {"installed", "manual"}
        assert content.plan.digest() == binding.plan_digest
        assert any(entry.path == "root.txt" for entry in content.manifest.entries)
        boundary = await session.scalar(
            select(SkillDeploymentDiscovery).where(
                SkillDeploymentDiscovery.operation_id == operation_id
            )
        )
        assert boundary is not None and boundary.resolved_digest == binding.plan_digest
        assert boundary.original_digest != binding.plan_digest
        intent = await NodeDeploymentTermination(session).request(
            takeover.node,
            binding.task_id,
            binding.attempt_id,
            SkillDeploymentTerminationRequest(lease_attempt=1, error_code="TRANSFER_FAILED"),
        )
        await NodeDeploymentTermination(session).confirm(
            takeover.node,
            binding.task_id,
            binding.attempt_id,
            SkillDeploymentTerminatedResult(
                intent=intent,
                drain=SkillDeploymentDrain(
                    version=1, binding=intent.binding, helper_receipt_id=uuid4()
                ),
            ),
        )
    async with takeover.library.database.begin() as session:
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        await retry_deployment(
            session,
            operation.user_id,
            operation_id,
            SkillDeploymentRetryRequest(
                idempotency_key="retry-discovered",
                expected_generation=operation.generation,
                targets=(
                    SkillRetryTarget(account_id=takeover.account, attempt_id=binding.attempt_id),
                ),
            ),
        )
    successor = await reserve(takeover, operation_id)
    assert successor.attempt_id != binding.attempt_id
    assert (
        successor.plan_digest == binding.plan_digest
        and successor.checkpoint_id == binding.checkpoint_id
    )
    async with takeover.library.database() as session:
        report = protection(
            await SkillRetentionRepository(session).load(takeover.library.owner),
            datetime.now(UTC),
        )
        assert receipt.checkpoint_id is not None
        assert report.reasons("checkpoint", receipt.checkpoint_id)


async def test_later_local_disable_is_real_supersession_not_another_discovery(
    takeover: TakeoverHarness,
) -> None:
    """
    后来的本地规则变更取代未完成操作，不把停用值倒灌到原解析。

    :param takeover (TakeoverHarness): 原始未接管账户
    """
    operation_id, _ = await discovered(takeover)
    async with takeover.library.database() as session:
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None
        original = saved_plans(
            operation,
            *await SkillDeploymentRepository(session).rows(operation.user_id, operation.id),
        )
        before = await execution_plans(session, original)
    changed = await takeover.library.execute(
        SkillRuleRequest(
            command="disable",
            skill="manual",
            scope=SkillScope(account_id=takeover.account),
            idempotency_key="disable-discovered",
            expected_generation=await takeover.library.generation(),
        )
    )
    async with takeover.library.database() as session:
        observed = await takeover.library.service(session).status(
            takeover.library.owner, operation_id
        )
        assert (
            observed.status == "superseded" and observed.data.replacement_id == changed.operation_id
        )
        assert await execution_plans(session, original) == before
    with pytest.raises(SkillContentError) as error:
        await reserve(takeover, operation_id)
    assert error.value.code == "OPERATION_SUPERSEDED"


async def test_post_takeover_acceptance_does_not_get_discovery_permission(
    takeover: TakeoverHarness,
) -> None:
    """
    已管理账户后续受理只保存当时全部来源，不能追加另一轮发现。

    :param takeover (TakeoverHarness): 原始未接管账户
    """
    operation_id, _ = await discovered(takeover)
    unchanged = await takeover.library.execute(
        SkillRuleRequest(
            command="enable",
            skill="installed",
            idempotency_key="same-after-discovery",
            expected_generation=await takeover.library.generation(),
        )
    )
    async with takeover.library.database() as session:
        boundary = await session.scalar(
            select(SkillDeploymentDiscovery).where(
                SkillDeploymentDiscovery.operation_id == unchanged.operation_id
            )
        )
        assert boundary is None
        prior = await takeover.library.service(session).status(takeover.library.owner, operation_id)
        assert prior.status == "preparing" and prior.data.replacement_id is None
