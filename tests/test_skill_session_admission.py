"""
验证真实会话入口的受管准备、能力选择、精确任务快照和持久冲突边界。
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_migration_conflicts import pending
from test_skill_snapshots import prepared as prepared

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import Node, NodeTask, Session, ToolAccount, User, Workspace
from agent_remote_server.models.skill_preparation import (
    SkillBranchPreparation,
    SkillEffectiveBranch,
)
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.services.sessions import ToolSessionService


def capability() -> dict[str, object]:
    """
    模拟独立探测通过的完整后端报告，不改变真实 Node 广告。

    :return dict[str, object]: 完整测试能力
    """
    return {
        "protocol_version": 1,
        "manifest_version": 1,
        "writable_copies": True,
        "finalization": True,
        "recovery": True,
    }


async def ready(state: RuntimeHarness, backend: str = "native") -> None:
    """
    将真实归属测试账户和节点设为可调度，原会话保持终态。

    :param state (RuntimeHarness): 已接管账户
    :param backend (str): 本次受管测试后端
    """
    async with state.database.begin() as session:
        account = await session.get(ToolAccount, state.account)
        node = await session.get(Node, state.node)
        original = await session.get(Session, state.session)
        assert account is not None and node is not None and original is not None
        workspace = await session.get(Workspace, original.workspace_id)
        assert workspace is not None
        workspace.remote_path = "/var/lib/agent-remote/workspaces/test"
        account.status = "active"
        account.region_code = state.owner.hex
        node.region_code = state.owner.hex
        account.runtime_backend = backend
        account.affinity_node_id = node.id
        node.supported_tool_types = ["claude"]
        node.allowed_runtime_backends = [backend]
        node.default_runtime_backend = backend
        node.runtime_capabilities = {
            "backends": [backend],
            "skill_manager": {backend: capability()},
        }
        node.last_heartbeat_at = datetime.now(UTC)
        node.version = "test-release"
        original.status = "stopped"


async def launch(state: RuntimeHarness, root: Path) -> Session:
    """
    调用现有完整会话服务，由其决定成功提交或持久冲突拒绝。

    :param state (RuntimeHarness): 账户身份
    :param root (Path): 私有卷
    :return Session: 完整受理的新会话
    """
    async with state.database() as session:
        user = await session.get(User, state.owner)
        original = await session.get(Session, state.session)
        assert user is not None and original is not None
        return await ToolSessionService(
            session,
            Settings(
                secret_key="test", skill_manager_enabled=True, skill_storage_root=root / "objects"
            ),
        ).create_session(
            user=user,
            tool_type="claude",
            tool_account_id=state.account,
            workspace_id=original.workspace_id,
            project_key=original.project_key,
            argv=[],
        )


@pytest.mark.parametrize("backend", ["native", "docker_sandbox"])
async def test_managed_create_prepares_account_and_binds_exact_snapshot_task(
    prepared: RuntimeHarness, tmp_path: Path, backend: str
) -> None:
    """
    会话、准备任务、快照及有效使用记录必须一起持久化且身份完全匹配。

    :param prepared (RuntimeHarness): 未初始化目标
    :param tmp_path (Path): 私有卷
    :param backend (str): 控制面固定后端，测试不代表运行时验收
    """
    await ready(prepared, backend)
    created = await launch(prepared, tmp_path)
    async with prepared.database() as session:
        snapshot = await session.scalar(
            select(SessionSkillSnapshot).where(SessionSkillSnapshot.session_id == created.id)
        )
        assert snapshot is not None and snapshot.library_generation == 1
        task = await session.get(NodeTask, snapshot.prepare_task_id)
        assert task is not None and task.payload["skill_manager"] == {
            "protocol_version": 1,
            "manifest_version": 1,
            "snapshot_id": str(snapshot.id),
            "task_id": str(task.id),
        }
        assert snapshot.node_id == created.node_id == prepared.node
        assert snapshot.runtime_backend == created.runtime_backend == backend
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillEffectiveBranch)
                .where(SkillEffectiveBranch.user_id == prepared.owner)
            )
            == 1
        )


async def test_admission_conflict_is_durable_without_session_task_or_usage(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    自动准备失败必须保留可解决的独立迁移，并且重试不无限创建相同比较。

    :param stopped (RuntimeHarness): 有旧版状态的账户
    :param tmp_path (Path): 内容卷
    """
    await pending(stopped, tmp_path, "forward")
    await ready(stopped)
    async with stopped.database() as session:
        before = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Session, NodeTask, SessionSkillSnapshot, SkillEffectiveBranch)
        ]
    with pytest.raises(ApiError) as first:
        await launch(stopped, tmp_path)
    assert first.value.code == "STATE_MIGRATION_REQUIRED"
    with pytest.raises(ApiError) as second:
        await launch(stopped, tmp_path)
    assert first.value.details == second.value.details
    identities = first.value.details["migration_ids"]
    assert isinstance(identities, list) and len(identities) == 1
    async with stopped.database() as session:
        assert [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Session, NodeTask, SessionSkillSnapshot, SkillEffectiveBranch)
        ] == before
        rows = (
            await session.scalars(
                select(SkillBranchPreparation).where(
                    SkillBranchPreparation.user_id == stopped.owner
                )
            )
        ).all()
        assert any(str(row.id) in identities and row.status == "conflicted" for row in rows)
