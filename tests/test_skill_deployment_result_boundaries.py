"""
验证损坏终态和旧领取确认不能绕过独立部署回执边界。
"""

from pathlib import Path

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import delete, select
from test_skill_content_service import database as database
from test_skill_deployment_authorization import leased
from test_skill_deployment_dispatch import settings
from test_skill_deployment_results import proposal
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import NodeTask, NodeTaskResult
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_results import NodeDeploymentResults


@pytest.mark.parametrize("damage", ["missing", "duplicate", "poll", "result", "logical", "type"])
async def test_damaged_terminal_history_is_not_absence_or_replay(
    prepared: RuntimeHarness, tmp_path: Path, damage: str
) -> None:
    """
    历史确认必须一致，损坏不能被当成安全重试或第二个成功结果。

    :param prepared (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    :param damage (str): 损坏的终态证据
    """
    binding = await leased(prepared, tmp_path)
    result = await proposal(prepared, tmp_path, binding)
    async with prepared.database.begin() as session:
        await NodeDeploymentResults(session, settings(tmp_path)).confirm(
            prepared.node, binding.task_id, binding.attempt_id, result
        )
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, binding.task_id)
        saved = await session.scalar(
            select(NodeTaskResult).where(NodeTaskResult.node_task_id == binding.task_id)
        )
        assert task is not None and saved is not None
        if damage == "missing":
            await session.execute(delete(NodeTaskResult).where(NodeTaskResult.id == saved.id))
        elif damage == "duplicate":
            session.add(
                NodeTaskResult(
                    node_task_id=task.id,
                    task_id=task.task_id,
                    status="succeeded",
                    result=saved.result,
                    finished_at=saved.finished_at,
                )
            )
        elif damage == "poll":
            task.retry_count += 1
        elif damage == "result":
            saved.result = {"unrelated": True}
        elif damage == "logical":
            saved.task_id = "changed"
        else:
            task.task_type = "reconcile_state"
    for action in ("inspect", "confirm"):
        async with prepared.database.begin() as session:
            with pytest.raises(SkillContentError):
                await getattr(NodeDeploymentResults(session, settings(tmp_path)), action)(
                    prepared.node, binding.task_id, binding.attempt_id, result
                )


async def test_reissue_observation_fences_old_result_without_renewing(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    新轮次观察旧提议未提交之后，旧轮次不能再提交本地成功。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    binding = await leased(prepared, tmp_path)
    result = await proposal(prepared, tmp_path, binding)
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, binding.task_id)
        assert task is not None
        task.retry_count = 2
        deadline = task.lease_until
        assert deadline is not None
    async with prepared.database.begin() as session:
        observation = await NodeDeploymentResults(session, settings(tmp_path)).inspect(
            prepared.node, binding.task_id, binding.attempt_id, result
        )
        assert not observation.accepted and observation.current_lease_attempt == 2
        task = await session.get(NodeTask, binding.task_id)
        assert task is not None and task.lease_until is not None
        assert task.lease_until == deadline.replace(tzinfo=task.lease_until.tzinfo)
    async with prepared.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await NodeDeploymentResults(session, settings(tmp_path)).confirm(
                prepared.node, binding.task_id, binding.attempt_id, result
            )
        assert error.value.code == "DEPLOYMENT_LEASE_CHANGED"
