"""
验证运行态表在真实外键下拒绝错绑、提前释放与不干净发布。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness, runtime
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database as database

from agent_remote_server.models import Node, NodeTask, Session, ToolAccount, User
from agent_remote_server.models.skill_snapshots import (
    SessionSkillSnapshot,
    SessionSkillSnapshotItem,
    SkillFinalization,
)
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
)
from agent_remote_server.models.skill_storage import SkillStoredTree


@pytest.fixture
async def state(database: async_sessionmaker[AsyncSession], tmp_path: Path) -> RuntimeHarness:
    """
    构建每个用例独立的运行态。

    :param database (async_sessionmaker[AsyncSession]): 数据库工厂
    :param tmp_path (Path): 内容卷
    :return RuntimeHarness: 运行态身份
    """
    return await runtime(database, tmp_path)


@pytest.mark.parametrize("target", ["user", "account", "node", "task", "session", "tree"])
async def test_pending_snapshot_prevents_cascading_deletion(
    state: RuntimeHarness, target: str
) -> None:
    """
    父记录级联删除不能移除未完成快照或其内容。

    :param state (RuntimeHarness): 完整快照
    :param target (str): 删除目标
    """
    statements = {
        "user": delete(User).where(User.id == state.owner),
        "account": delete(ToolAccount).where(ToolAccount.id == state.account),
        "node": delete(Node).where(Node.id == state.node),
        "task": delete(NodeTask).where(NodeTask.id == state.task),
        "session": delete(Session).where(Session.id == state.session),
        "tree": delete(SkillStoredTree).where(
            SkillStoredTree.user_id == state.owner, SkillStoredTree.category == "state"
        ),
    }
    with pytest.raises(IntegrityError):
        async with state.database.begin() as session:
            await session.execute(statements[target])


@pytest.mark.parametrize(
    "target",
    [
        "directory_item",
        "branch_directory",
        "branch_foreign",
        "snapshot_item",
        "snapshot_node",
        "snapshot_task",
        "snapshot_account",
        "snapshot_user",
        "detach_pending",
    ],
)
async def test_runtime_bindings_cannot_be_substituted(
    state: RuntimeHarness, tmp_path: Path, target: str
) -> None:
    """
    同时验证真实存在的另一身份，避免仅测试不存在的随机外键。

    :param state (RuntimeHarness): 原始快照
    :param tmp_path (Path): 内容卷
    :param target (str): 被替换的绑定
    """
    other = await runtime(state.database, tmp_path / "other")
    statements = {
        "directory_item": update(AccountSkillDirectoryState)
        .where(AccountSkillDirectoryState.account_id == state.account)
        .values(head_checkpoint_id=state.item),
        "branch_directory": update(AccountSkillState)
        .where(AccountSkillState.id == state.state)
        .values(head_checkpoint_id=state.directory),
        "branch_foreign": update(AccountSkillState)
        .where(AccountSkillState.id == state.state)
        .values(head_checkpoint_id=other.item),
        "snapshot_item": update(SessionSkillSnapshot)
        .where(SessionSkillSnapshot.id == state.snapshot)
        .values(starting_checkpoint_id=state.item),
        "snapshot_node": update(SessionSkillSnapshot)
        .where(SessionSkillSnapshot.id == state.snapshot)
        .values(node_id=other.node),
        "snapshot_task": update(SessionSkillSnapshot)
        .where(SessionSkillSnapshot.id == state.snapshot)
        .values(prepare_task_id=other.task),
        "snapshot_account": update(SessionSkillSnapshot)
        .where(SessionSkillSnapshot.id == state.snapshot)
        .values(account_id=other.account),
        "snapshot_user": update(SessionSkillSnapshot)
        .where(SessionSkillSnapshot.id == state.snapshot)
        .values(user_id=other.owner),
        "detach_pending": update(SessionSkillSnapshot)
        .where(SessionSkillSnapshot.id == state.snapshot)
        .values(session_id=None),
    }
    with pytest.raises(IntegrityError):
        async with state.database.begin() as session:
            await session.execute(statements[target])


async def test_snapshot_item_requires_its_exact_branch(
    state: RuntimeHarness, tmp_path: Path
) -> None:
    """
    子项不能把其他账户 checkpoint 伪装成原分支的起始内容。

    :param state (RuntimeHarness): 原始快照
    :param tmp_path (Path): 内容卷
    """
    other = await runtime(state.database, tmp_path / "other")
    with pytest.raises(IntegrityError):
        async with state.database.begin() as session:
            session.add(
                SessionSkillSnapshotItem(
                    snapshot_id=state.snapshot,
                    state_id=state.state,
                    user_id=state.owner,
                    account_id=state.account,
                    entry_name="learning",
                    state_epoch=1,
                    checkpoint_id=other.item,
                    resolution_json={},
                )
            )


@pytest.mark.parametrize("status", ["persisted", "published", "conflicted"])
async def test_unclean_finalization_cannot_publish(state: RuntimeHarness, status: str) -> None:
    """
    完整内容已保存也不能把异常退出归类为干净发布。

    :param state (RuntimeHarness): 原始快照
    :param status (str): 禁止的不干净状态
    """
    with pytest.raises(IntegrityError):
        async with state.database.begin() as session:
            session.add(
                SkillFinalization(
                    user_id=state.owner,
                    account_id=state.account,
                    node_id=state.node,
                    snapshot_id=state.snapshot,
                    idempotency_key=str(uuid4()),
                    request_digest="a" * 64,
                    incoming_digest=state.tree,
                    tree_digest=state.tree,
                    checkpoint_id=state.directory,
                    unclean=True,
                    status=status,
                )
            )


async def test_finalization_checkpoint_must_reference_incoming_tree(state: RuntimeHarness) -> None:
    """
    不能用另一份已保存的合法树为传入内容提供持久化证明。

    :param state (RuntimeHarness): 原始快照
    """
    other_digest = "b" * 64
    async with state.database.begin() as session:
        session.add(
            SkillStoredTree(
                user_id=state.owner,
                category="state",
                digest=other_digest,
                manifest_json={},
                total_bytes=0,
            )
        )
    with pytest.raises(IntegrityError):
        async with state.database.begin() as session:
            session.add(
                SkillFinalization(
                    user_id=state.owner,
                    account_id=state.account,
                    node_id=state.node,
                    snapshot_id=state.snapshot,
                    idempotency_key=str(uuid4()),
                    request_digest="a" * 64,
                    incoming_digest=other_digest,
                    tree_digest=other_digest,
                    checkpoint_id=state.directory,
                    unclean=False,
                    status="persisted",
                )
            )


async def test_retained_snapshot_survives_explicit_session_deletion(state: RuntimeHarness) -> None:
    """
    显式保留并解除关系后删除展示记录，审计身份与内容仍在。

    :param state (RuntimeHarness): 原始快照
    """
    async with state.database.begin() as session:
        await session.execute(
            update(SessionSkillSnapshot)
            .where(SessionSkillSnapshot.id == state.snapshot)
            .values(status="retained", session_id=None)
        )
        await session.execute(delete(Session).where(Session.id == state.session))
    async with state.database() as session:
        snapshot = await session.get(SessionSkillSnapshot, state.snapshot)
        assert snapshot is not None and snapshot.session_reference_id == state.session
        assert snapshot.tree_digest == state.tree


async def test_checkpoint_retirement_preserves_audit_digest(state: RuntimeHarness) -> None:
    """
    退役同时清除树指针，不能把仍保留标记的内容引用单独置空。

    :param state (RuntimeHarness): 原始快照
    """
    with pytest.raises(IntegrityError):
        async with state.database.begin() as session:
            await session.execute(
                update(SkillCheckpoint)
                .where(SkillCheckpoint.id == state.item)
                .values(tree_digest=None)
            )
    async with state.database.begin() as session:
        await session.execute(
            update(SkillCheckpoint)
            .where(SkillCheckpoint.id == state.item)
            .values(tree_digest=None, retained=False)
        )
    async with state.database() as session:
        checkpoint = await session.get(SkillCheckpoint, state.item)
        assert checkpoint is not None and checkpoint.content_digest == state.tree
        assert checkpoint.tree_digest is None and not checkpoint.retained
