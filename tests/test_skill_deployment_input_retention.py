"""
验证重试固定原输入、配置替代不冒充排空、终态元数据不阻止内容退役。
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import ingest, publish
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_deployment_authorization import leased
from test_skill_deployment_dispatch import settings
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.models.skill_state import SkillCheckpoint
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
from agent_remote_server.schemas.skill_library import SkillRuleRequest
from agent_remote_server.schemas.skill_results import SkillMutationData
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_authorization import authorize_deployment
from agent_remote_server.services.skills.deployment_dispatch import SkillDeploymentDispatch
from agent_remote_server.services.skills.deployment_retry import retry_deployment
from agent_remote_server.services.skills.deployment_termination import NodeDeploymentTermination
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def finish(state: RuntimeHarness, binding: SkillDeploymentTask, *, retryable: bool) -> None:
    """
    通过生产撤权及终态服务提交模拟 Helper 排空证据，验证重试及保活。

    :param state (RuntimeHarness): 真实数据库测试身份
    :param binding (SkillDeploymentTask): 原完整绑定
    :param retryable (bool): 是否仍允许恢复原任务输入
    """
    async with state.database.begin() as session:
        task = await session.get(NodeTask, binding.task_id)
        assert task is not None
        service = NodeDeploymentTermination(session)
        intent = await service.request(
            state.node,
            binding.task_id,
            binding.attempt_id,
            SkillDeploymentTerminationRequest(
                lease_attempt=task.retry_count,
                error_code="TRANSFER_FAILED" if retryable else "AUTHORIZATION_DENIED",
            ),
        )
        await service.confirm(
            state.node,
            binding.task_id,
            binding.attempt_id,
            SkillDeploymentTerminatedResult(
                intent=intent,
                drain=SkillDeploymentDrain(
                    version=1,
                    binding=intent.binding,
                    helper_receipt_id=uuid4(),
                ),
            ),
        )


async def retry(state: RuntimeHarness, binding: SkillDeploymentTask) -> UUID:
    """
    使用生产重试入口追加后继，原操作与配置代数保持不变。

    :param state (RuntimeHarness): 原用户账户
    :param binding (SkillDeploymentTask): 刚结束的原尝试
    :return UUID: 唯一后继身份
    """
    async with state.database.begin() as session:
        operation = await session.get(SkillOperation, binding.operation_id)
        assert operation is not None
        result = await retry_deployment(
            session,
            state.owner,
            binding.operation_id,
            SkillDeploymentRetryRequest(
                idempotency_key=str(uuid4()),
                expected_generation=operation.generation,
                targets=(
                    SkillRetryTarget(account_id=state.account, attempt_id=binding.attempt_id),
                ),
            ),
        )
        attempt_id = SkillMutationData.model_validate(result.result_json).targets[0].attempt_id
        assert attempt_id is not None
        return attempt_id


async def test_retry_reuses_original_directory_after_actual_published_head_progress(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    真实会话发布新学习后，重试仍消费第一次固定目录而不重新准备当前 head。

    :param stopped (RuntimeHarness): 原受管会话
    :param tmp_path (Path): 私有卷
    """
    original = await leased(stopped, tmp_path)
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"new learning"})
    )
    await finish(stopped, original, retryable=True)
    attempt = await retry(stopped, original)
    async with stopped.database.begin() as session:
        successor = await SkillDeploymentDispatch(session, settings(tmp_path)).reserve(
            stopped.owner, original.operation_id, stopped.account, attempt
        )
        assert isinstance(successor, SkillDeploymentTask)
        assert successor.task_id != original.task_id
        assert successor.checkpoint_id == original.checkpoint_id
        assert successor.content_digest == original.content_digest
        tree = await content_service(session, tmp_path).read_tree(
            stopped.owner, "state", successor.content_digest
        )
        assert "learning/memory" not in {entry.path for entry in tree.entries}
        assert {"learning/SKILL.md", "notes/SKILL.md"}.issubset(
            {entry.path for entry in tree.entries}
        )


@pytest.mark.parametrize("supersede", [False, True])
async def test_active_input_survives_replacement_then_terminal_metadata_allows_retirement(
    prepared: RuntimeHarness, tmp_path: Path, supersede: bool
) -> None:
    """
    活动输入不可剪除，替代不释放它；真实终态后可退役内容且保留原任务摘要。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param supersede (bool): 是否先改变有效配置
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
        async with prepared.database.begin() as session:
            with pytest.raises(SkillContentError) as error:
                await authorize_deployment(
                    session,
                    settings(tmp_path),
                    prepared.node,
                    binding.task_id,
                    binding.attempt_id,
                    1,
                )
            assert error.value.code == "OPERATION_SUPERSEDED"
    key = RetentionKey("checkpoint", str(binding.checkpoint_id))
    async with prepared.database.begin() as session:
        index = await SkillRetentionRepository(session).load(prepared.owner)
        assert protection(index, datetime.now(UTC)).reasons(
            "checkpoint", binding.checkpoint_id
        ) == {"pending_operation"}
        with pytest.raises(SkillContentError) as error:
            await SkillHistoryRetirementService(session, SkillStoragePolicy()).retire(
                prepared.owner, prepared.account, (key,), all_unreferenced=True
            )
        assert error.value.code == "STATE_PROTECTED"
    await finish(prepared, binding, retryable=False)
    async with prepared.database.begin() as session:
        checkpoint = await session.get(SkillCheckpoint, binding.checkpoint_id)
        assert checkpoint is not None and checkpoint.retention_released_at is not None
        assert await SkillHistoryRetirementService(session, SkillStoragePolicy()).retire(
            prepared.owner, prepared.account, (key,), all_unreferenced=True
        ) == (key,)
        await session.refresh(checkpoint)
        assert not checkpoint.retained and checkpoint.tree_digest is None
        saved = await session.get(SkillDeploymentTask, binding.attempt_id)
        assert saved is not None and saved.content_digest == binding.content_digest


async def test_retryable_failure_keeps_original_input_protected_between_retries(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    原失败尚未提交重试时不能提前开始输入回收时钟。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    binding = await leased(prepared, tmp_path)
    await finish(prepared, binding, retryable=True)
    async with prepared.database.begin() as session:
        index = await SkillRetentionRepository(session).load(prepared.owner)
        assert protection(index, datetime.now(UTC)).reasons("checkpoint", binding.checkpoint_id)
        checkpoint = await session.get(SkillCheckpoint, binding.checkpoint_id)
        assert checkpoint is not None and checkpoint.retention_released_at is None
    await retry(prepared, binding)
    async with prepared.database.begin() as session:
        index = await SkillRetentionRepository(session).load(prepared.owner)
        assert protection(index, datetime.now(UTC)).reasons("checkpoint", binding.checkpoint_id)
