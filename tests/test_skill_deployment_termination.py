"""
验证先撤权再排空的部署失败协议、内容保护与原终态重放。
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from deployment_attempt_support import observe
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_deployment_authorization import leased
from test_skill_deployment_dispatch import settings
from test_skill_deployment_input_retention import retry
from test_skill_deployment_results import proposal
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import NodeTask, NodeTaskResult
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_deployment_termination import (
    SkillDeploymentDrain,
    SkillDeploymentTerminatedResult,
    SkillDeploymentTerminationIntent,
    SkillDeploymentTerminationRequest,
)
from agent_remote_server.schemas.skill_library import SkillRuleRequest
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_content import NodeDeploymentContent
from agent_remote_server.services.skills.deployment_dispatch import SkillDeploymentDispatch
from agent_remote_server.services.skills.deployment_results import NodeDeploymentResults
from agent_remote_server.services.skills.deployment_termination import NodeDeploymentTermination
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


def drained(intent: SkillDeploymentTerminationIntent) -> SkillDeploymentTerminatedResult:
    """
    构造精确原指令的模拟本地凭据，不代表真实 Helper 已执行。

    :param intent (SkillDeploymentTerminationIntent): 实际持久撤权指令
    :return SkillDeploymentTerminatedResult: 绑定完整原身份的终态提案
    """
    return SkillDeploymentTerminatedResult(
        intent=intent,
        drain=SkillDeploymentDrain(
            version=1,
            binding=intent.binding,
            helper_receipt_id=uuid4(),
        ),
    )


async def test_intent_fences_success_and_content_but_preserves_input(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    撤权先提交时拒绝迟到成功和续租，但尚无排空回执不能释放输入。

    :param prepared (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    """
    binding = await leased(prepared, tmp_path)
    success = await proposal(prepared, tmp_path, binding)
    request = SkillDeploymentTerminationRequest(lease_attempt=1, error_code="TRANSFER_FAILED")
    async with prepared.database.begin() as session:
        service = NodeDeploymentTermination(session)
        assert await service.lookup(prepared.node, binding.task_id, binding.attempt_id) is None
        intent = await service.request(prepared.node, binding.task_id, binding.attempt_id, request)
    async with prepared.database.begin() as session:
        service = NodeDeploymentTermination(session)
        assert (
            await service.request(prepared.node, binding.task_id, binding.attempt_id, request)
            == intent
        )
        assert await service.lookup(prepared.node, binding.task_id, binding.attempt_id) == intent
        absent = await service.inspect(
            prepared.node, binding.task_id, binding.attempt_id, drained(intent)
        )
        assert not absent.accepted and absent.task_status == "leased"
        assert await session.scalar(select(func.count()).select_from(NodeTaskResult)) == 0
        index = await SkillRetentionRepository(session).load(prepared.owner)
        assert protection(index, datetime.now(UTC)).reasons("checkpoint", binding.checkpoint_id)
        with pytest.raises(SkillContentError) as error:
            await NodeDeploymentResults(session, settings(tmp_path)).confirm(
                prepared.node, binding.task_id, binding.attempt_id, success
            )
        assert error.value.code == "DEPLOYMENT_REVOKED"
    async with prepared.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await NodeDeploymentContent(session, settings(tmp_path)).authorize(
                prepared.node, binding.task_id, binding.attempt_id, 1
            )
        assert error.value.code == "DEPLOYMENT_REVOKED"


@pytest.mark.parametrize("supersede", [False, True])
async def test_nonretryable_drain_commits_once_and_replays_after_retirement(
    prepared: RuntimeHarness,
    tmp_path: Path,
    supersede: bool,
) -> None:
    """
    真实替代选择取消终态，普通失败选择失败；精确重放不复活已退役内容。

    :param prepared (RuntimeHarness): 原始部署身份
    :param tmp_path (Path): 私有内容卷
    :param supersede (bool): 是否先提交真实配置替代
    """
    binding = await leased(prepared, tmp_path)
    if supersede:
        library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
        await library.execute(
            SkillRuleRequest(
                command="disable",
                skill="notes",
                idempotency_key=str(uuid4()),
                expected_generation=await library.generation(),
            )
        )
    request = SkillDeploymentTerminationRequest(lease_attempt=1, error_code="AUTHORIZATION_DENIED")
    async with prepared.database.begin() as session:
        intent = await NodeDeploymentTermination(session).request(
            prepared.node, binding.task_id, binding.attempt_id, request
        )
        assert intent.outcome == ("superseded" if supersede else "failed") and not intent.retryable
    result = drained(intent)
    async with prepared.database.begin() as session:
        observed = await NodeDeploymentTermination(session).confirm(
            prepared.node, binding.task_id, binding.attempt_id, result
        )
        assert observed.accepted and observed.task_status == (
            "cancelled" if supersede else "failed"
        )
        checkpoint = await session.get(SkillCheckpoint, binding.checkpoint_id)
        assert checkpoint is not None and checkpoint.retention_released_at is not None
        key = RetentionKey("checkpoint", str(binding.checkpoint_id))
        assert await SkillHistoryRetirementService(session, SkillStoragePolicy()).retire(
            prepared.owner, prepared.account, (key,), all_unreferenced=True
        ) == (key,)
    async with prepared.database.begin() as session:
        service = NodeDeploymentTermination(session)
        assert (
            await service.confirm(prepared.node, binding.task_id, binding.attempt_id, result)
            == observed
        )
        assert (
            await service.inspect(prepared.node, binding.task_id, binding.attempt_id, result)
            == observed
        )
        assert await session.scalar(select(func.count()).select_from(NodeTaskResult)) == 1
        with pytest.raises(SkillContentError):
            await service.confirm(
                prepared.node, binding.task_id, binding.attempt_id, drained(intent)
            )


async def test_retryable_drain_preserves_input_and_historical_replay_after_successor(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    排空失败允许原计划重试；后继存在后重放仍只核对前序终态。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有内容卷
    """
    binding = await leased(prepared, tmp_path)
    async with prepared.database.begin() as session:
        service = NodeDeploymentTermination(session)
        intent = await service.request(
            prepared.node,
            binding.task_id,
            binding.attempt_id,
            SkillDeploymentTerminationRequest(lease_attempt=1, error_code="DEPLOYMENT_INTERRUPTED"),
        )
        result = drained(intent)
        observed = await service.confirm(prepared.node, binding.task_id, binding.attempt_id, result)
        checkpoint = await session.get(SkillCheckpoint, binding.checkpoint_id)
        assert checkpoint is not None and checkpoint.retention_released_at is None
    successor = await retry(prepared, binding)
    async with prepared.database.begin() as session:
        next_task = await SkillDeploymentDispatch(session, settings(tmp_path)).reserve(
            prepared.owner, binding.operation_id, prepared.account, successor
        )
        assert (
            isinstance(next_task, SkillDeploymentTask)
            and next_task.checkpoint_id == binding.checkpoint_id
        )
        assert (
            await NodeDeploymentTermination(session).confirm(
                prepared.node, binding.task_id, binding.attempt_id, result
            )
            == observed
        )
        operation = await session.get(SkillOperation, binding.operation_id)
        assert operation is not None and operation.status == "preparing"


async def test_success_wins_before_revocation_and_rejects_fake_supersession(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    已提交成功不能产生排空指令，未替代操作不能伪报替代原因。

    :param prepared (RuntimeHarness): 原始部署身份
    :param tmp_path (Path): 私有内容卷
    """
    binding = await leased(prepared, tmp_path)
    success = await proposal(prepared, tmp_path, binding)
    async with prepared.database.begin() as session:
        with pytest.raises(SkillContentError):
            await NodeDeploymentTermination(session).request(
                prepared.node,
                binding.task_id,
                binding.attempt_id,
                SkillDeploymentTerminationRequest(
                    lease_attempt=1, error_code="OPERATION_SUPERSEDED"
                ),
            )
        await NodeDeploymentResults(session, settings(tmp_path)).confirm(
            prepared.node, binding.task_id, binding.attempt_id, success
        )
    async with prepared.database.begin() as session:
        with pytest.raises(SkillContentError):
            await NodeDeploymentTermination(session).request(
                prepared.node,
                binding.task_id,
                binding.attempt_id,
                SkillDeploymentTerminationRequest(lease_attempt=1, error_code="TRANSFER_FAILED"),
            )


async def test_expired_poll_can_revoke_but_old_poll_cannot_revoke_new_execution(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    撤权可安全结束过期原领取，但替换后的轮次不能被旧错误报告消费。

    :param prepared (RuntimeHarness): 原部署身份
    :param tmp_path (Path): 私有内容卷
    """
    binding = await leased(prepared, tmp_path)
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, binding.task_id)
        assert task is not None
        task.status, task.lease_until, task.retry_count = "expired", None, 2
    async with prepared.database.begin() as session:
        service = NodeDeploymentTermination(session)
        with pytest.raises(SkillContentError):
            await service.request(
                prepared.node,
                binding.task_id,
                binding.attempt_id,
                SkillDeploymentTerminationRequest(lease_attempt=1, error_code="TRANSFER_FAILED"),
            )
        intent = await service.request(
            prepared.node,
            binding.task_id,
            binding.attempt_id,
            SkillDeploymentTerminationRequest(lease_attempt=2, error_code="TRANSFER_FAILED"),
        )
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, binding.task_id)
        assert task is not None
        task.retry_count = 3
    result = drained(intent)
    async with prepared.database.begin() as session:
        observed = await NodeDeploymentTermination(session).confirm(
            prepared.node, binding.task_id, binding.attempt_id, result
        )
        assert observed.accepted and observed.current_lease_attempt == 3


async def test_failed_task_flag_without_drain_cannot_authorize_successor(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    失败标志和可重试投影不能替代真实已提交排空回执。

    :param prepared (RuntimeHarness): 原始部署身份
    :param tmp_path (Path): 私有内容卷
    """
    binding = await leased(prepared, tmp_path)
    async with prepared.database.begin() as session, retention_mutation(session, prepared.owner):
        task = await session.get(NodeTask, binding.task_id)
        operation = await session.get(SkillOperation, binding.operation_id)
        assert task is not None and operation is not None
        task.status, task.lease_until = "failed", None
        await observe(session, operation, "failed", retryable=True, error_code="TRANSFER_FAILED")
    successor = await retry(prepared, binding)
    async with prepared.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillDeploymentDispatch(session, settings(tmp_path)).reserve(
                prepared.owner, binding.operation_id, prepared.account, successor
            )
        assert error.value.code == "DEPLOYMENT_TASK_CHANGED"
