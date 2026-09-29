"""
验证轮询批次公平性、数据库并发和预约失败的原子边界。
"""

import asyncio
from pathlib import Path
from uuid import UUID

import pytest
from skill_deployment_scheduling_support import accept, compatible, poll
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_deployment_dispatch import settings
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Node, NodeTask, ToolAccount, User
from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, SkillCheckpoint
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.services.skills import deployment_scheduling
from agent_remote_server.services.skills.deployment_dispatch import SkillDeploymentDispatch
from agent_remote_server.services.skills.deployment_scheduling import schedule_deployments
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def test_bounded_rotation_reaches_targets_behind_waiting_takeovers(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    首轮最多四个候选，未完成接管不会永久遮住排在后面的账户。

    :param prepared (RuntimeHarness): 原用户节点
    :param tmp_path (Path): 私有卷
    """
    await compatible(prepared)
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    accounts = [prepared.account] + [await library.account() for _ in range(5)]
    async with prepared.database.begin() as session:
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert directory is not None
        directory.mode, directory.head_checkpoint_id = "legacy", None
        for identity in accounts:
            account = await session.get(ToolAccount, identity)
            assert account is not None
            account.status = "active"
            account.affinity_node_id, account.runtime_backend = prepared.node, "native"
    result = await accept(prepared, tmp_path)
    assert len(result.data.targets) == 6
    assert len(await poll(prepared, tmp_path)) == 4
    async with prepared.database() as session:
        assert await session.scalar(select(func.count()).select_from(SkillAccountTakeover)) == 4
    assert len(await poll(prepared, tmp_path)) == 2
    async with prepared.database() as session:
        assert await session.scalar(select(func.count()).select_from(SkillAccountTakeover)) == 6
        assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 0


async def test_concurrent_polls_reserve_and_lease_one_original_task(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    独立连接同时取得旧候选时仍只保存一个输入和一次有效租约。

    :param prepared (RuntimeHarness): 原账户环境
    :param tmp_path (Path): 私有卷
    """
    async with prepared.database() as session:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL transaction locks")
    await compatible(prepared)
    result = await accept(prepared, tmp_path)
    polls = await asyncio.gather(poll(prepared, tmp_path), poll(prepared, tmp_path))
    tasks = [task for batch in polls for task in batch]
    assert len(tasks) == 1 and tasks[0].retry_count == 1
    async with prepared.database() as session:
        bindings = list(
            await session.scalars(
                select(SkillDeploymentTask).where(
                    SkillDeploymentTask.account_id == prepared.account
                )
            )
        )
        assert len(bindings) == 1 and bindings[0].operation_id == result.operation_id


async def test_unexpected_late_failure_rolls_back_and_keeps_candidate_retryable(
    prepared: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    真实任务保存后异常不能留下半个预约，也不能消费原受理。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param monkeypatch (pytest.MonkeyPatch): 有界事务故障注入
    """
    await compatible(prepared)
    result = await accept(prepared, tmp_path)
    original = SkillDeploymentDispatch.reserve

    async def interrupted(
        self: SkillDeploymentDispatch,
        user_id: UUID,
        operation_id: UUID,
        account_id: UUID,
        attempt_id: UUID,
    ) -> None:
        """
        在真实预约完成后中断外层事务。

        :param user_id (UUID): 原用户
        :param operation_id (UUID): 原配置
        :param account_id (UUID): 原账户
        :param attempt_id (UUID): 原尝试
        """
        await original(self, user_id, operation_id, account_id, attempt_id)
        raise RuntimeError("injected post-reservation interruption")

    async with prepared.database() as session:
        counts = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (NodeTask, SkillCheckpoint)
        ]
    with monkeypatch.context() as patch:
        patch.setattr(SkillDeploymentDispatch, "reserve", interrupted)
        with pytest.raises(RuntimeError, match="post-reservation"):
            await poll(prepared, tmp_path)
    async with prepared.database() as session:
        assert [
            await session.scalar(select(func.count()).select_from(model))
            for model in (NodeTask, SkillCheckpoint)
        ] == counts
        attempt = await session.get(SkillDeploymentAttempt, result.data.targets[0].attempt_id)
        assert attempt is not None and attempt.status == "pending"
        assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 0
    assert len(await poll(prepared, tmp_path)) == 1


async def test_stale_candidate_after_other_reservation_cannot_change_existing_task(
    prepared: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    先读取候选再由另一事务预约，旧扫描重入时必须重新检查真实任务库存。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param monkeypatch (pytest.MonkeyPatch): 固定一次旧候选读数
    """
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from agent_remote_server.repositories.skill_deployment_scheduling import (
        DeploymentCandidate,
        deployment_candidates,
    )

    await compatible(prepared)
    await accept(prepared, tmp_path)
    async with prepared.database() as session:
        stale = await deployment_candidates(session, prepared.node, 4)
    tasks = await poll(prepared, tmp_path)
    assert len(tasks) == 1

    async def old_scan(
        session: AsyncSession, node_id: UUID, limit: int
    ) -> tuple[DeploymentCandidate, ...]:
        """
        重放锁外读数，不绕过锁内任务检查。

        :param session (AsyncSession): 当前轮询事务
        :param node_id (UUID): 原节点
        :param limit (int): 有界候选数
        :return tuple[DeploymentCandidate, ...]: 已失效的旧扫描
        """
        return stale

    with monkeypatch.context() as patch:
        patch.setattr(deployment_scheduling, "deployment_candidates", old_scan)
        async with prepared.database() as session:
            await schedule_deployments(session, settings(tmp_path), prepared.node)
    async with prepared.database() as session:
        assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 1
        task = await session.get(NodeTask, tasks[0].id)
        assert task is not None and task.status == "leased" and task.retry_count == 1


@pytest.mark.parametrize(
    "change,code,status",
    [
        ("binding", "DEPLOYMENT_BINDING_CHANGED", "failed"),
        ("account", "ACCOUNT_NOT_AVAILABLE", "failed"),
        ("owner", "AUTHORIZATION_DENIED", "failed"),
        ("quota", "QUOTA_EXCEEDED", "failed"),
    ],
)
async def test_pre_dispatch_rejection_records_exact_failure_without_task(
    prepared: RuntimeHarness, tmp_path: Path, change: str, code: str, status: str
) -> None:
    """
    受理后的真实撤权或配额失败只改变未执行目标，不留下部分输入或任务。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param change (str): 本次变更的授权边界
    :param code (str): 稳定错误分类
    :param status (str): 预期目标终态
    """
    await compatible(prepared)
    result = await accept(prepared, tmp_path)
    policy = settings(tmp_path)
    async with prepared.database.begin() as session:
        node = await session.get(Node, prepared.node)
        account = await session.get(ToolAccount, prepared.account)
        owner = await session.get(User, prepared.owner)
        assert node is not None and account is not None and owner is not None
        if change == "binding":
            account.runtime_backend = "docker_sandbox"
        elif change == "account":
            account.status = "disabled"
        elif change == "owner":
            owner.status = "disabled"
        else:
            policy.skill_storage_policy = SkillStoragePolicy(checkpoint_bytes=1)
        checkpoints = await session.scalar(select(func.count()).select_from(SkillCheckpoint))
    async with prepared.database() as session:
        await schedule_deployments(session, policy, prepared.node)
    async with prepared.database() as session:
        attempt = await session.get(SkillDeploymentAttempt, result.data.targets[0].attempt_id)
        assert attempt is not None and (attempt.status, attempt.error_code) == (status, code)
        assert attempt.retryable == (change == "quota")
        assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(SkillCheckpoint)) == checkpoints
        )
