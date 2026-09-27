"""
验证普通轮询从原受理生成精确任务及离线、撤权和替代交错。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from skill_deployment_scheduling_support import accept, compatible, poll
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_deployment_dispatch import settings
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration_conflicts import pending as migration_pending
from test_skill_migration_resolution_drafts import edit_request
from test_skill_migration_resolution_service import resolve
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Node, NodeTask
from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.services.skills.deployment_scheduling import schedule_deployments


async def test_poll_schedules_once_and_redelivers_original_binding(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    普通轮询自动预约并租用原任务，过期重投不生成新的完整输入。

    :param prepared (RuntimeHarness): 已接管原账户
    :param tmp_path (Path): 私有卷
    """
    await compatible(prepared)
    result = await accept(prepared, tmp_path)
    tasks = await poll(prepared, tmp_path)
    deployed = [task for task in tasks if task.task_type == "prepare_account_skills"]
    assert len(deployed) == 1 and deployed[0].retry_count == 1
    task = deployed[0]
    assert task.payload["operation_id"] == str(result.operation_id)
    assert await poll(prepared, tmp_path) == []
    async with prepared.database.begin() as session:
        saved = await session.get(NodeTask, task.id)
        assert saved is not None
        saved.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    repeated = await poll(prepared, tmp_path)
    assert len(repeated) == 1 and repeated[0].id == task.id and repeated[0].retry_count == 2
    async with prepared.database() as session:
        assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 1
        binding = await session.scalar(select(SkillDeploymentTask))
        assert binding is not None and binding.attempt_id == result.data.targets[0].attempt_id


async def test_offline_pending_waits_then_schedules_on_fresh_report(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    旧心跳允许保存配置，但直到原节点恢复才产生执行权。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    await compatible(prepared)
    async with prepared.database.begin() as session:
        node = await session.get(Node, prepared.node)
        assert node is not None
        node.status, node.last_heartbeat_at = "offline", datetime.now(UTC) - timedelta(days=1)
    result = await accept(prepared, tmp_path)
    assert await poll(prepared, tmp_path) == []
    async with prepared.database() as session:
        attempt = await session.get(SkillDeploymentAttempt, result.data.targets[0].attempt_id)
        assert attempt is not None and attempt.status == "pending"
        assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 0
    await compatible(prepared)
    assert any(
        task.task_type == "prepare_account_skills" for task in await poll(prepared, tmp_path)
    )


async def test_superseded_unbound_attempt_terminates_without_inventing_drain(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    新配置替代尚未下发的旧受理，只为最新配置生成任务。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    await compatible(prepared)
    first = await accept(prepared, tmp_path)
    second = await accept(prepared, tmp_path, "newer")
    tasks = await poll(prepared, tmp_path)
    assert len(tasks) == 1 and tasks[0].payload["operation_id"] == str(second.operation_id)
    async with prepared.database() as session:
        old = await session.get(SkillDeploymentAttempt, first.data.targets[0].attempt_id)
        operation = await session.get(SkillOperation, first.operation_id)
        assert old is not None and old.status == "superseded"
        assert operation is not None and operation.replacement_id == second.operation_id
        assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 1


async def test_scheduler_never_terminates_superseded_bound_attempt(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    已经生成任务的旧尝试仍等待其永久排空，调度不能代替 Helper。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    await compatible(prepared)
    first = await accept(prepared, tmp_path)
    tasks = await poll(prepared, tmp_path)
    original = next(task for task in tasks if task.task_type == "prepare_account_skills")
    await accept(prepared, tmp_path, "newer")
    await poll(prepared, tmp_path)
    async with prepared.database() as session:
        old = await session.get(SkillDeploymentAttempt, first.data.targets[0].attempt_id)
        saved = await session.get(NodeTask, original.id)
        assert old is not None and old.status == "pending"
        assert saved is not None and saved.status == "leased"


async def test_conflicted_attempt_stays_visible_and_polling_is_idempotent(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    迁移分歧保存 needs_resolution，多次轮询不创建任务或伪造就绪。

    :param stopped (RuntimeHarness): 有学习状态分歧的账户
    :param tmp_path (Path): 私有卷
    """
    await migration_pending(stopped, tmp_path, "forward")
    await compatible(stopped)
    from uuid import uuid4

    from agent_remote_server.schemas.skill_library import SkillRuleRequest
    from agent_remote_server.services.skills.library import SkillLibraryService
    from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    request = SkillRuleRequest(
        idempotency_key=str(uuid4()),
        expected_generation=await library.generation(),
        command="enable",
        skill="learning",
    )
    async with stopped.database.begin() as session:
        result = await SkillLibraryService(
            session, PrivateObjectStore(tmp_path / "objects"), settings(tmp_path)
        ).execute(stopped.owner, request)
    for _ in range(2):
        assert await poll(stopped, tmp_path) == []
        async with stopped.database() as session:
            assert result.operation_id is not None
            observed = (
                await LibraryHarness(stopped.database, tmp_path, stopped.owner)
                .service(session)
                .status(stopped.owner, result.operation_id)
            )
            assert observed.status == "needs_resolution"
            assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 0
    from agent_remote_server.models.skill_preparation import SkillBranchPreparation

    async with stopped.database() as session:
        conflicts = list(
            await session.scalars(
                select(SkillBranchPreparation).where(SkillBranchPreparation.status == "conflicted")
            )
        )
    for conflict in conflicts:
        await resolve(stopped, tmp_path, conflict.id, edit_request())
    tasks = await poll(stopped, tmp_path)
    assert len(tasks) == 1 and tasks[0].payload["operation_id"] == str(result.operation_id)


async def test_disabled_scheduler_does_not_consume_pending_acceptance(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    关闭开关保留原待执行受理和内容，不隐式取消或派发。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    await compatible(prepared)
    result = await accept(prepared, tmp_path)
    policy = settings(tmp_path)
    policy.skill_manager_enabled = False
    async with prepared.database() as session:
        await schedule_deployments(session, policy, prepared.node)
        attempt = await session.get(SkillDeploymentAttempt, result.data.targets[0].attempt_id)
        assert attempt is not None and attempt.status == "pending"
        assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 0
