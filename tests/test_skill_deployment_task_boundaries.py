"""
验证部署绑定的数据库隔离、保存点和真实并发预约。
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from deployment_attempt_support import observe
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_deployment_authorization import leased
from test_skill_deployment_dispatch import pending, settings
from test_skill_deployment_input_retention import retry
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_dispatch import SkillDeploymentDispatch
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.clocks import retention_mutation


@pytest.mark.parametrize(
    "field",
    [
        "user_id",
        "operation_id",
        "account_id",
        "node_id",
        "task_id",
        "checkpoint_id",
        "content_digest",
        "checkpoint_scope",
    ],
)
async def test_database_rejects_foreign_binding_fields(
    prepared: RuntimeHarness, tmp_path: Path, field: str
) -> None:
    """
    不依赖服务层检查，数据库拒绝替换原任务、尝试或完整内容身份。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param field (str): 被破坏的单一归属字段
    """
    binding = await leased(prepared, tmp_path)
    value = (
        "f" * 64
        if field == "content_digest"
        else "item"
        if field == "checkpoint_scope"
        else uuid4()
    )
    with pytest.raises(IntegrityError):
        async with prepared.database.begin() as session:
            await session.execute(
                update(SkillDeploymentTask)
                .where(SkillDeploymentTask.attempt_id == binding.attempt_id)
                .values({field: value})
            )


async def test_failed_attempt_does_not_release_or_overlap_still_active_task(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    单独写失败投影不是执行排空证据，内容继续保护且新尝试不能与旧任务并发。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    binding = await leased(prepared, tmp_path)
    async with prepared.database.begin() as session, retention_mutation(session, prepared.owner):
        operation = await session.get(SkillOperation, binding.operation_id)
        assert operation is not None
        await observe(session, operation, "failed", retryable=True, error_code="TRANSFER_FAILED")
    successor = await retry(prepared, binding)
    async with prepared.database.begin() as session:
        index = await SkillRetentionRepository(session).load(prepared.owner)
        assert protection(index, datetime.now(UTC)).reasons("checkpoint", binding.checkpoint_id)
        with pytest.raises(SkillContentError) as error:
            await SkillDeploymentDispatch(session, settings(tmp_path)).reserve(
                prepared.owner, binding.operation_id, prepared.account, successor
            )
        assert error.value.code == "DEPLOYMENT_TASK_CHANGED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillDeploymentTask)
                .where(SkillDeploymentTask.operation_id == binding.operation_id)
            )
            == 1
        )


async def test_postgres_concurrent_reservation_creates_one_task_and_one_input(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    两个独立事务竞争原尝试时由用户锁串行，后者只能读取第一个完整预约。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    async with prepared.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
    operation_id, attempt_id = await pending(prepared, tmp_path)

    async def reserve() -> SkillDeploymentTask:
        """
        独立连接执行并提交同一个原始尝试。

        :return SkillDeploymentTask: 原始任务绑定
        """
        async with prepared.database.begin() as session:
            result = await SkillDeploymentDispatch(session, settings(tmp_path)).reserve(
                prepared.owner, operation_id, prepared.account, attempt_id
            )
            assert isinstance(result, SkillDeploymentTask)
            return result

    first, second = await asyncio.wait_for(asyncio.gather(reserve(), reserve()), timeout=30)
    assert first.task_id == second.task_id and first.checkpoint_id == second.checkpoint_id
    async with prepared.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillDeploymentTask)
                .where(SkillDeploymentTask.operation_id == operation_id)
            )
            == 1
        )
        assert await session.get(SkillCheckpoint, first.checkpoint_id) is not None
