"""
验证后台部署准备不借用会话、不宣称执行完成，并固定精确任务输入。
"""

from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest
from deployment_attempt_support import observe
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration_conflicts import pending as migration_pending
from test_skill_session_admission import capability, ready
from test_skill_snapshots import prepared as prepared

from agent_remote_server.config import Settings
from agent_remote_server.models import Node, NodeTask, Session
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.models.skill_preparation import (
    SkillBranchPreparation,
    SkillEffectiveBranch,
)
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, SkillCheckpoint
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_dispatch import SkillDeploymentDispatch
from agent_remote_server.services.skills.takeover_admission import SkillTakeoverPending


def settings(root: Path) -> Settings:
    """
    使用测试私有卷及明确启用的服务开关。

    :param root (Path): 临时目录
    :return Settings: 测试部署配置
    """
    return Settings(
        secret_key="test", skill_manager_enabled=True, skill_storage_root=root / "objects"
    )


async def pending(state: RuntimeHarness, root: Path) -> tuple[UUID, UUID]:
    """
    构造尚未公开启用的部署受理，不代表真实 Node 已具备执行能力。

    :param state (RuntimeHarness): 已接管测试账户
    :param root (Path): 私有卷
    :return tuple[UUID, UUID]: 原操作和精确尝试
    """
    await ready(state)
    async with state.database.begin() as session:
        node = await session.get(Node, state.node)
        assert node is not None
        node.runtime_capabilities = {
            "backends": ["native"],
            "skill_manager": {"native": capability() | {"deployment_protocol_version": 1}},
        }
    library = LibraryHarness(state.database, root, state.owner)
    accepted = await library.add(await library.candidate(name="notes"))
    async with state.database.begin() as session:
        operation = await session.get(SkillOperation, accepted.operation_id)
        assert operation is not None
        current = await observe(session, operation, "pending")
        return operation.id, current[state.account].id


async def test_reservation_fixes_complete_input_without_session_or_effective_use(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    保存完整目录及精确任务，重复预约返回同一输入且不伪造实际使用。

    :param prepared (RuntimeHarness): 尚未初始化分支的账户
    :param tmp_path (Path): 私有内容卷
    """
    operation_id, attempt_id = await pending(prepared, tmp_path)
    async with prepared.database.begin() as session:
        counts = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Session, SessionSkillSnapshot, SkillEffectiveBranch)
        ]
        service = SkillDeploymentDispatch(session, settings(tmp_path))
        first = await service.reserve(prepared.owner, operation_id, prepared.account, attempt_id)
        assert isinstance(first, SkillDeploymentTask)
        second = await service.reserve(prepared.owner, operation_id, prepared.account, attempt_id)
        assert isinstance(second, SkillDeploymentTask) and second.task_id == first.task_id
        checkpoint = await session.get(SkillCheckpoint, first.checkpoint_id)
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        task = await session.get(NodeTask, first.task_id)
        operation = await session.get(SkillOperation, operation_id)
        assert checkpoint is not None and checkpoint.scope == "directory"
        assert directory is not None and directory.head_checkpoint_id != checkpoint.id
        assert task is not None and task.status == "pending"
        assert operation is not None and operation.status == "preparing"
        assert [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Session, SessionSkillSnapshot, SkillEffectiveBranch)
        ] == counts


async def test_invalid_backend_capability_does_not_prepare_or_enqueue(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    通常会话能力不能冒充尚未接入的后台部署协议。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    operation_id, attempt_id = await pending(prepared, tmp_path)
    await ready(prepared)
    async with prepared.database.begin() as session:
        before = await session.scalar(select(func.count()).select_from(SkillCheckpoint))
        with pytest.raises(SkillContentError, match="deployment protocol"):
            await SkillDeploymentDispatch(session, settings(tmp_path)).reserve(
                prepared.owner, operation_id, prepared.account, attempt_id
            )
        assert await session.scalar(select(func.count()).select_from(SkillCheckpoint)) == before
        assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 0


async def test_late_failure_rolls_back_input_task_preparation_and_references(
    prepared: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    绑定真实刷新后注入故障，外层捕获并提交不能留下部分任务或迁移。

    :param prepared (RuntimeHarness): 未初始化账户
    :param tmp_path (Path): 私有卷
    :param monkeypatch (pytest.MonkeyPatch): 最后一步故障
    """
    operation_id, attempt_id = await pending(prepared, tmp_path)
    models = (SkillCheckpoint, NodeTask, SkillDeploymentTask, SkillBranchPreparation)
    async with prepared.database.begin() as session:
        before = [await session.scalar(select(func.count()).select_from(model)) for model in models]
        original = session.flush

        async def flush(objects: Sequence[object] | None = None) -> None:
            """
            在完整部署任务绑定真实入库后失败。

            :param objects (Sequence[object] | None): 原刷新对象
            """
            inserted = any(isinstance(row, SkillDeploymentTask) for row in session.new)
            await original(objects)
            if inserted:
                raise RuntimeError("late deployment failure")

        with monkeypatch.context() as patch:
            patch.setattr(session, "flush", flush)
            with pytest.raises(RuntimeError, match="late deployment"):
                await SkillDeploymentDispatch(session, settings(tmp_path)).reserve(
                    prepared.owner, operation_id, prepared.account, attempt_id
                )
        assert [
            await session.scalar(select(func.count()).select_from(model)) for model in models
        ] == before


async def test_migration_conflict_is_retained_without_task_or_false_readiness(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    正常迁移冲突保存原三侧，部署阶段明确需要解决且不创建 Node 任务。

    :param stopped (RuntimeHarness): 已有学习分歧的账户
    :param tmp_path (Path): 私有卷
    """
    await migration_pending(stopped, tmp_path, "forward")
    operation_id, attempt_id = await pending(stopped, tmp_path)
    async with stopped.database.begin() as session:
        result = await SkillDeploymentDispatch(session, settings(tmp_path)).reserve(
            stopped.owner, operation_id, stopped.account, attempt_id
        )
        assert isinstance(result, tuple) and len(result) == 1
        migration = await session.get(SkillBranchPreparation, result[0])
        assert migration is not None and migration.status == "conflicted"
        operation = await session.get(SkillOperation, operation_id)
        assert operation is not None and operation.status == "needs_resolution"
        assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 0


async def test_legacy_account_reserves_original_takeover_without_deployment_task(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    旧目录先进入已有接管协议，后台预约不能跳过真实捕获或重复创建接管。

    :param prepared (RuntimeHarness): 原账户及节点
    :param tmp_path (Path): 私有卷
    """
    operation_id, attempt_id = await pending(prepared, tmp_path)
    async with prepared.database.begin() as session:
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert directory is not None
        directory.mode, directory.head_checkpoint_id = "legacy", None
    async with prepared.database.begin() as session:
        dispatch = SkillDeploymentDispatch(session, settings(tmp_path))
        first = await dispatch.reserve(prepared.owner, operation_id, prepared.account, attempt_id)
        second = await dispatch.reserve(prepared.owner, operation_id, prepared.account, attempt_id)
        assert isinstance(first, SkillTakeoverPending) and first == second
        assert await session.scalar(select(func.count()).select_from(SkillDeploymentTask)) == 0
