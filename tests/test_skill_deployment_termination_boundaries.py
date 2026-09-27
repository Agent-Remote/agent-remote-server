"""
验证撤权与成功的真实竞争、终态损坏及保存点回滚。
"""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_deployment_authorization import leased
from test_skill_deployment_dispatch import settings
from test_skill_deployment_results import proposal
from test_skill_deployment_termination import drained
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import NodeTask, NodeTaskResult
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.schemas.skill_deployment_termination import (
    SkillDeploymentTerminationRequest,
)
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_results import NodeDeploymentResults
from agent_remote_server.services.skills.deployment_termination import NodeDeploymentTermination


async def test_drain_confirmation_flush_failure_rolls_back_terminal_state(
    prepared: RuntimeHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    已实际插入结果后失败仍回滚全部终态，已提交撤权保持有效。

    :param prepared (RuntimeHarness): 原部署身份
    :param tmp_path (Path): 私有内容卷
    :param monkeypatch (pytest.MonkeyPatch): 刷新故障注入
    """
    binding = await leased(prepared, tmp_path)
    async with prepared.database.begin() as session:
        intent = await NodeDeploymentTermination(session).request(
            prepared.node,
            binding.task_id,
            binding.attempt_id,
            SkillDeploymentTerminationRequest(lease_attempt=1, error_code="AUTHORIZATION_DENIED"),
        )
    result = drained(intent)
    async with prepared.database.begin() as session:
        original = session.flush

        async def fail_after_result(*args: object, **kwargs: object) -> None:
            """
            仅在真实结果行已落入事务后模拟保存失败。

            :param args (object): 未使用的位置参数
            :param kwargs (object): 未使用的命名参数
            """
            await original()
            if await session.scalar(select(func.count()).select_from(NodeTaskResult)):
                raise RuntimeError("injected terminal failure")

        monkeypatch.setattr(session, "flush", fail_after_result)
        with pytest.raises(RuntimeError, match="injected"):
            await NodeDeploymentTermination(session).confirm(
                prepared.node, binding.task_id, binding.attempt_id, result
            )
        monkeypatch.setattr(session, "flush", original)
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, binding.task_id)
        operation = await session.get(SkillOperation, binding.operation_id)
        assert task is not None and task.status == "leased"
        assert operation is not None and operation.status == "preparing"
        assert await session.scalar(select(func.count()).select_from(NodeTaskResult)) == 0
        assert (
            await NodeDeploymentTermination(session).lookup(
                prepared.node, binding.task_id, binding.attempt_id
            )
            == intent
        )


@pytest.mark.parametrize("change", ["missing", "duplicate", "poll", "lease", "intent", "drain"])
async def test_corrupt_terminal_history_never_becomes_absence(
    prepared: RuntimeHarness,
    tmp_path: Path,
    change: str,
) -> None:
    """
    原终态缺失或漂移不能触发重新提交或覆盖。

    :param prepared (RuntimeHarness): 原部署身份
    :param tmp_path (Path): 私有内容卷
    :param change (str): 损坏的终态字段
    """
    binding = await leased(prepared, tmp_path)
    async with prepared.database.begin() as session:
        service = NodeDeploymentTermination(session)
        intent = await service.request(
            prepared.node,
            binding.task_id,
            binding.attempt_id,
            SkillDeploymentTerminationRequest(lease_attempt=1, error_code="TRANSFER_FAILED"),
        )
        result = drained(intent)
        await service.confirm(prepared.node, binding.task_id, binding.attempt_id, result)
    async with prepared.database.begin() as session:
        row = await session.scalar(
            select(NodeTaskResult).where(NodeTaskResult.node_task_id == binding.task_id)
        )
        task = await session.get(NodeTask, binding.task_id)
        assert row is not None and task is not None
        if change == "missing":
            await session.delete(row)
        elif change == "duplicate":
            session.add(
                NodeTaskResult(
                    node_task_id=task.id,
                    task_id=task.task_id,
                    status=row.status,
                    result=row.result,
                    error=None,
                    finished_at=row.finished_at,
                )
            )
        elif change == "poll":
            task.retry_count += 1
        elif change == "lease":
            task.lease_until = row.finished_at
        elif change == "intent":
            result = result.model_copy(
                update={"intent": intent.model_copy(update={"intent_id": uuid4()})}
            )
        else:
            result = result.model_copy(
                update={"drain": result.drain.model_copy(update={"helper_receipt_id": uuid4()})}
            )
    async with prepared.database.begin() as session:
        with pytest.raises(SkillContentError):
            await NodeDeploymentTermination(session).inspect(
                prepared.node, binding.task_id, binding.attempt_id, result
            )


async def test_postgres_success_and_revocation_have_one_winner(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    独立连接下成功与首次撤权共用锁序，不能同时提交两种授权事实。

    :param prepared (RuntimeHarness): PostgreSQL 原部署身份
    :param tmp_path (Path): 私有内容卷
    """
    async with prepared.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL locks")
    binding = await leased(prepared, tmp_path)
    success = await proposal(prepared, tmp_path, binding)

    async def compete(revoke: bool) -> bool:
        """
        独立事务竞争原始任务的一种提交。

        :param revoke (bool): 是否请求永久撤权
        :return bool: 是否成功提交
        """
        try:
            async with prepared.database.begin() as session:
                if revoke:
                    await NodeDeploymentTermination(session).request(
                        prepared.node,
                        binding.task_id,
                        binding.attempt_id,
                        SkillDeploymentTerminationRequest(
                            lease_attempt=1, error_code="TRANSFER_FAILED"
                        ),
                    )
                else:
                    await NodeDeploymentResults(session, settings(tmp_path)).confirm(
                        prepared.node, binding.task_id, binding.attempt_id, success
                    )
            return True
        except SkillContentError:
            return False

    winners = await asyncio.wait_for(asyncio.gather(compete(True), compete(False)), 30)
    assert sum(winners) == 1
    async with prepared.database.begin() as session:
        intent = await NodeDeploymentTermination(session).lookup(
            prepared.node, binding.task_id, binding.attempt_id
        )
        assert (intent is not None) == winners[0]
        assert await session.scalar(select(func.count()).select_from(NodeTaskResult)) == int(
            winners[1]
        )


@pytest.mark.parametrize("different", [False, True])
async def test_postgres_drain_confirmation_is_single_immutable_result(
    prepared: RuntimeHarness,
    tmp_path: Path,
    different: bool,
) -> None:
    """
    同一提案并发只写一次，不同排空回执只有一个可以提交。

    :param prepared (RuntimeHarness): PostgreSQL 原部署身份
    :param tmp_path (Path): 私有内容卷
    :param different (bool): 第二个提案是否故意替换本地回执
    """
    async with prepared.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL locks")
    binding = await leased(prepared, tmp_path)
    async with prepared.database.begin() as session:
        intent = await NodeDeploymentTermination(session).request(
            prepared.node,
            binding.task_id,
            binding.attempt_id,
            SkillDeploymentTerminationRequest(lease_attempt=1, error_code="TRANSFER_FAILED"),
        )
    first = drained(intent)
    second = drained(intent) if different else first

    async def confirm(alternate: bool) -> bool:
        """
        独立连接提交给定的原排空结果。

        :param alternate (bool): 是否提交第二个提案
        :return bool: 是否提交或精确重放成功
        """
        try:
            async with prepared.database.begin() as session:
                await NodeDeploymentTermination(session).confirm(
                    prepared.node,
                    binding.task_id,
                    binding.attempt_id,
                    second if alternate else first,
                )
            return True
        except SkillContentError:
            return False

    outcomes = await asyncio.wait_for(asyncio.gather(confirm(False), confirm(True)), 30)
    assert sum(outcomes) == (1 if different else 2)
    async with prepared.database() as session:
        assert await session.scalar(select(func.count()).select_from(NodeTaskResult)) == 1
