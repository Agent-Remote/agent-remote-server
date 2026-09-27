"""
验证部署结果、原始准备和就绪投影原子提交，重放不重建当前输入。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_deployment_authorization import leased
from test_skill_deployment_dispatch import settings
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Node, NodeTask, NodeTaskResult, Session, User
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.models.skill_preparation import SkillEffectiveBranch
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, SkillCheckpoint
from agent_remote_server.schemas.skill_deployment_content import SkillDeploymentIdentity
from agent_remote_server.schemas.skill_deployment_result import (
    SkillDeploymentPreparation,
    SkillDeploymentPreparedResult,
)
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_content import NodeDeploymentContent
from agent_remote_server.services.skills.deployment_digest import deployment_input_digest
from agent_remote_server.services.skills.deployment_results import NodeDeploymentResults
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def proposal(
    state: RuntimeHarness, root: Path, binding: SkillDeploymentTask
) -> SkillDeploymentPreparedResult:
    """
    从真实保存的完整部署输入构造模拟 Helper 证据，保持所有原始字段。

    :param state (RuntimeHarness): 原账户测试状态
    :param root (Path): 私有卷目录
    :param binding (SkillDeploymentTask): 实际预约的任务
    :return SkillDeploymentPreparedResult: 原完整输入的准备结果
    """
    async with state.database.begin() as session:
        content = await NodeDeploymentContent(session, settings(root)).describe(
            state.node, binding.task_id, binding.attempt_id, 1
        )
        return SkillDeploymentPreparedResult(
            lease_attempt=1,
            preparation=SkillDeploymentPreparation(
                version=1,
                binding=SkillDeploymentIdentity.model_validate(
                    content.model_dump(include=set(SkillDeploymentIdentity.model_fields))
                ),
                input_digest=deployment_input_digest(content),
                directory_epoch=content.directory_epoch,
                generation=content.plan.generation,
                helper_receipt_id=uuid4(),
            ),
        )


async def test_success_is_atomic_and_replay_survives_input_retirement(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    就绪只改变部署任务和投影，历史结果在实际目录输入退役后仍可恢复。

    :param prepared (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    """
    binding = await leased(prepared, tmp_path)
    result = await proposal(prepared, tmp_path, binding)
    async with prepared.database.begin() as session:
        counts = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Session, SessionSkillSnapshot, SkillEffectiveBranch)
        ]
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert directory is not None
        head = directory.head_checkpoint_id
        service = NodeDeploymentResults(session, settings(tmp_path))
        absent = await service.inspect(prepared.node, binding.task_id, binding.attempt_id, result)
        assert not absent.accepted and absent.task_status == "leased"
        accepted = await service.confirm(prepared.node, binding.task_id, binding.attempt_id, result)
        assert accepted.accepted and accepted.task_status == "succeeded"
        assert directory.head_checkpoint_id == head
        assert counts == [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Session, SessionSkillSnapshot, SkillEffectiveBranch)
        ]
        operation = await session.get(SkillOperation, binding.operation_id)
        checkpoint = await session.get(SkillCheckpoint, binding.checkpoint_id)
        assert operation is not None and operation.status == "ready"
        assert checkpoint is not None and checkpoint.retention_released_at is not None
    async with prepared.database.begin() as session:
        selected = (RetentionKey("checkpoint", str(binding.checkpoint_id)),)
        assert (
            await SkillHistoryRetirementService(session, SkillStoragePolicy()).retire(
                prepared.owner, prepared.account, selected, all_unreferenced=True
            )
            == selected
        )
        node = await session.get(Node, prepared.node)
        assert node is not None
        node.runtime_capabilities = {}
    configured = settings(tmp_path)
    configured.skill_manager_enabled = False
    async with prepared.database.begin() as session:
        service = NodeDeploymentResults(session, configured)
        assert (
            await service.confirm(prepared.node, binding.task_id, binding.attempt_id, result)
            == accepted
        )
        assert (
            await service.inspect(prepared.node, binding.task_id, binding.attempt_id, result)
        ).accepted
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NodeTaskResult)
                .where(NodeTaskResult.node_task_id == binding.task_id)
            )
            == 1
        )
        checkpoint = await session.get(SkillCheckpoint, binding.checkpoint_id)
        assert checkpoint is not None and not checkpoint.retained and checkpoint.tree_digest is None


@pytest.mark.parametrize("change", ["lease", "helper", "digest", "epoch", "generation", "binding"])
async def test_different_terminal_proposal_never_overwrites_original(
    prepared: RuntimeHarness, tmp_path: Path, change: str
) -> None:
    """
    已接受结果中的任一原始字段不能被另一轮次或另一回执替换。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有内容卷
    :param change (str): 修改的结果字段
    """
    binding = await leased(prepared, tmp_path)
    result = await proposal(prepared, tmp_path, binding)
    async with prepared.database.begin() as session:
        await NodeDeploymentResults(session, settings(tmp_path)).confirm(
            prepared.node, binding.task_id, binding.attempt_id, result
        )
    data = result.model_dump(mode="json")
    if change == "lease":
        data["lease_attempt"] = 2
    elif change == "binding":
        data["preparation"]["binding"]["checkpoint_id"] = str(uuid4())
    else:
        field, value = {
            "helper": ("helper_receipt_id", str(uuid4())),
            "digest": ("input_digest", "a" * 64),
            "epoch": ("directory_epoch", result.preparation.directory_epoch + 1),
            "generation": ("generation", result.preparation.generation + 1),
        }[change]
        data["preparation"][field] = value
    changed = SkillDeploymentPreparedResult.model_validate(data)
    for action in ("confirm", "inspect"):
        async with prepared.database.begin() as session:
            service = NodeDeploymentResults(session, settings(tmp_path))
            with pytest.raises(SkillContentError):
                await getattr(service, action)(
                    prepared.node, binding.task_id, binding.attempt_id, changed
                )


@pytest.mark.parametrize(
    "change", ["expired", "reissued", "owner", "epoch", "capability", "input", "disabled"]
)
async def test_first_result_requires_current_complete_authority(
    prepared: RuntimeHarness, tmp_path: Path, change: str
) -> None:
    """
    即使 Helper 曾准备成功，当前授权丢失也不能发布就绪。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param change (str): 受理前撤销的边界
    """
    binding = await leased(prepared, tmp_path)
    result = await proposal(prepared, tmp_path, binding)
    configured = settings(tmp_path)
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, binding.task_id)
        assert task is not None
        if change == "expired":
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "reissued":
            task.retry_count = 2
        elif change == "owner":
            owner = await session.get(User, prepared.owner)
            assert owner is not None
            owner.status = "disabled"
        elif change == "epoch":
            directory = await session.get(AccountSkillDirectoryState, prepared.account)
            assert directory is not None
            directory.epoch += 1
        elif change == "capability":
            node = await session.get(Node, prepared.node)
            assert node is not None
            node.runtime_capabilities = {}
        elif change == "disabled":
            configured.skill_manager_enabled = False
        else:
            result = result.model_copy(
                update={
                    "preparation": result.preparation.model_copy(update={"input_digest": "a" * 64})
                }
            )
    async with prepared.database.begin() as session:
        with pytest.raises(SkillContentError):
            await NodeDeploymentResults(session, configured).confirm(
                prepared.node, binding.task_id, binding.attempt_id, result
            )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NodeTaskResult)
                .where(NodeTaskResult.node_task_id == binding.task_id)
            )
            == 0
        )
        task = await session.get(NodeTask, binding.task_id)
        assert task is not None and task.status == "leased"


async def test_publication_failure_rolls_back_result_and_readiness(
    prepared: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    在结果和阶段实际写入后注入异常，保存点必须恢复整个提交边界。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param monkeypatch (pytest.MonkeyPatch): 刷新故障注入
    """
    binding = await leased(prepared, tmp_path)
    result = await proposal(prepared, tmp_path, binding)
    async with prepared.database.begin() as session:
        original = session.flush

        async def fail_after_result(*args: object, **kwargs: object) -> None:
            """
            已保存原回执后模拟后续事务失败。

            :param args (object): 未使用的位置参数
            :param kwargs (object): 未使用的命名参数
            """
            await original()
            if await session.scalar(
                select(func.count())
                .select_from(NodeTaskResult)
                .where(NodeTaskResult.node_task_id == binding.task_id)
            ):
                raise RuntimeError("injected post-result failure")

        with monkeypatch.context() as patch:
            patch.setattr(session, "flush", fail_after_result)
            with pytest.raises(RuntimeError, match="post-result"):
                await NodeDeploymentResults(session, settings(tmp_path)).confirm(
                    prepared.node, binding.task_id, binding.attempt_id, result
                )
        task = await session.get(NodeTask, binding.task_id)
        operation = await session.get(SkillOperation, binding.operation_id)
        assert task is not None and task.status == "leased"
        assert operation is not None and operation.status == "preparing"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NodeTaskResult)
                .where(NodeTaskResult.node_task_id == binding.task_id)
            )
            == 0
        )
