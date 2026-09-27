"""
过期任务不是 Helper 排空证据，保留和重试必须继续使用原始任务约束。
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from deployment_attempt_support import observe
from skill_runtime_support import RuntimeHarness
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_deployment_authorization import leased
from test_skill_deployment_dispatch import settings
from test_skill_deployment_input_retention import retry
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_library import SkillRuleRequest
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_dispatch import SkillDeploymentDispatch
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@pytest.mark.parametrize("supersede", [False, True])
async def test_expired_task_retains_input_after_nonretryable_projection(
    prepared: RuntimeHarness, tmp_path: Path, supersede: bool
) -> None:
    """
    即使配置替代或投影失败，领取过期也不能提前释放原目录及成员。

    :param prepared (RuntimeHarness): 原账户和真实数据库
    :param tmp_path (Path): 私有内容卷
    :param supersede (bool): 是否真实提交替代配置
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
    async with prepared.database.begin() as session, retention_mutation(session, prepared.owner):
        task = await session.get(NodeTask, binding.task_id)
        operation = await session.get(SkillOperation, binding.operation_id)
        assert task is not None and operation is not None
        task.status, task.lease_until = "expired", None
        await observe(
            session, operation, "failed", retryable=False, error_code="AUTHORIZATION_DENIED"
        )
    async with prepared.database.begin() as session:
        index = await SkillRetentionRepository(session).load(prepared.owner)
        assert protection(index, datetime.now(UTC)).reasons(
            "checkpoint", binding.checkpoint_id
        ) == {"pending_operation"}
        checkpoint = await session.get(SkillCheckpoint, binding.checkpoint_id)
        assert checkpoint is not None and checkpoint.retention_released_at is None
        with pytest.raises(SkillContentError) as error:
            await SkillHistoryRetirementService(session, SkillStoragePolicy()).retire(
                prepared.owner,
                prepared.account,
                (RetentionKey("checkpoint", str(binding.checkpoint_id)),),
                all_unreferenced=True,
            )
        assert error.value.code == "STATE_PROTECTED"


async def test_expired_predecessor_cannot_authorize_retry_execution(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    可以保存重试意图，但过期前序没有排空证据时不能创建并发执行任务。

    :param prepared (RuntimeHarness): 原部署身份
    :param tmp_path (Path): 私有内容卷
    """
    binding = await leased(prepared, tmp_path)
    async with prepared.database.begin() as session, retention_mutation(session, prepared.owner):
        task = await session.get(NodeTask, binding.task_id)
        operation = await session.get(SkillOperation, binding.operation_id)
        assert task is not None and operation is not None
        task.status, task.lease_until = "expired", None
        await observe(
            session, operation, "failed", retryable=True, error_code="DEPLOYMENT_INTERRUPTED"
        )
    successor = await retry(prepared, binding)
    async with prepared.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillDeploymentDispatch(session, settings(tmp_path)).reserve(
                prepared.owner,
                binding.operation_id,
                prepared.account,
                successor,
            )
        assert error.value.code == "DEPLOYMENT_TASK_CHANGED"


async def test_unknown_deployment_task_state_still_fails_retention(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    显式接受过期状态不允许未知状态被误当作已结束。

    :param prepared (RuntimeHarness): 原部署身份
    :param tmp_path (Path): 私有内容卷
    """
    binding = await leased(prepared, tmp_path)
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, binding.task_id)
        assert task is not None
        task.status = "unclassified"
        if session.get_bind().dialect.name == "postgresql":
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
        else:
            await session.flush()
            index = await SkillRetentionRepository(session).load(prepared.owner)
            with pytest.raises(ValueError, match="inconsistent ownership"):
                protection(index, datetime.now(UTC))
