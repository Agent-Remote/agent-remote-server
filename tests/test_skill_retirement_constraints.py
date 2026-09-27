"""
验证生成内容外键、活跃状态禁止退役与退役后的完整账户身份约束。
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from skill_publication_support import ingest, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_history_retirement import retire
from test_skill_library import LibraryHarness
from test_skill_migration_conflicts import pending as migration_pending
from test_skill_resolution_service import pending
from test_skill_snapshots import prepared as prepared
from test_skill_start_results import start_result

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, SkillCheckpoint
from agent_remote_server.skill_manager.retention.graph import RetentionKey


@pytest.mark.parametrize("kind", ["snapshot", "finalization", "publication", "migration"])
async def test_active_history_cannot_drop_generated_reference(
    stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    数据库本身拒绝绕过待收尾及未解决状态的内容引用。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param kind (str): 活跃历史类型
    """
    identity = stopped.snapshot
    if kind == "finalization":
        identity = await ingest(stopped, tmp_path, {})
    elif kind == "publication":
        identity = (await pending(stopped, tmp_path)).id
    elif kind == "migration":
        operation = (await migration_pending(stopped, tmp_path)).operation_id
        assert operation is not None
        identity = operation
    with pytest.raises(IntegrityError):
        async with stopped.database.begin() as session:
            row: (
                SessionSkillSnapshot
                | SkillFinalization
                | SkillPublication
                | SkillBranchPreparation
                | None
            )
            if kind == "snapshot":
                row = await session.get(SessionSkillSnapshot, identity)
            elif kind == "finalization":
                row = await session.get(SkillFinalization, identity)
            elif kind == "publication":
                row = await session.get(SkillPublication, identity)
            else:
                row = await session.get(SkillBranchPreparation, identity)
            assert row is not None
            row.content_retired_at = datetime.now(UTC)
            await session.flush()


async def test_retired_finalization_still_binds_exact_account_checkpoint(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    有效树外键变空后，同摘要另一账户的目录仍不能替换原收尾的身份。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    finalization_id = await ingest(stopped, tmp_path, {})
    publication = await publish(stopped, tmp_path, finalization_id)
    await retire(
        stopped,
        RetentionKey("publication", str(publication.id)),
        RetentionKey("finalization", str(finalization_id)),
        early=True,
    )
    account = await LibraryHarness(stopped.database, tmp_path, stopped.owner).account()
    async with stopped.database.begin() as session:
        finalization = await session.get(SkillFinalization, finalization_id)
        assert finalization is not None
        session.add(
            AccountSkillDirectoryState(
                user_id=stopped.owner, account_id=account, tool_type="claude"
            )
        )
        await session.flush()
        checkpoint = SkillCheckpoint(
            user_id=stopped.owner,
            account_id=account,
            scope="directory",
            tree_digest=finalization.incoming_digest,
            content_digest=finalization.incoming_digest,
        )
        session.add(checkpoint)
        await session.flush()
        other_checkpoint = checkpoint.id
    with pytest.raises(IntegrityError):
        async with stopped.database.begin() as session:
            finalization = await session.get(SkillFinalization, finalization_id)
            assert finalization is not None and finalization.retained_tree_digest is None
            finalization.checkpoint_id = other_checkpoint
            await session.flush()


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("valid_shape", [False, True])
async def test_late_runtime_result_cannot_reacquire_snapshot(
    stopped: RuntimeHarness, tmp_path: Path, retired: bool, valid_shape: bool
) -> None:
    """
    迟到启动回报不能重新保护已收尾快照，捕获错误并提交也保留原状态和时钟。

    :param stopped (RuntimeHarness): 原始账户与终态会话
    :param tmp_path (Path): 私有内容卷
    :param retired (bool): 是否已退役原快照
    :param valid_shape (bool): 是否提交字段完整的受管回报
    """
    from agent_remote_server.config import Settings
    from agent_remote_server.models import Node, NodeTask, NodeTaskResult, Session
    from agent_remote_server.services.nodes import NodeService
    from agent_remote_server.services.skills.content import SkillContentError

    finalization_id = await ingest(stopped, tmp_path, {})
    publication = await publish(stopped, tmp_path, finalization_id)
    if retired:
        await retire(
            stopped,
            RetentionKey("publication", str(publication.id)),
            RetentionKey("finalization", str(finalization_id)),
            RetentionKey("snapshot", str(stopped.snapshot)),
            early=True,
        )
    result: dict[str, object] = (
        await start_result(stopped)
        if valid_shape
        else {"session_id": str(stopped.session), "status": "running"}
    )
    async with stopped.database.begin() as session:
        node = await session.get(Node, stopped.node)
        task = await session.get(NodeTask, stopped.task)
        snapshot = await session.get(SessionSkillSnapshot, stopped.snapshot)
        assert node is not None and task is not None and snapshot is not None
        original_task_status = task.status
        original_snapshot_status = snapshot.status
        original_release = snapshot.retention_released_at
        assert original_release is not None
        with pytest.raises(SkillContentError) as error:
            await NodeService(session, Settings()).complete_task(
                node=node,
                task_id=task.task_id,
                result=result,
            )
        assert error.value.code == (
            "SKILL_START_LEASE_LOST" if valid_shape else "SKILL_START_RESULT_INVALID"
        )
    async with stopped.database() as session:
        tool_session = await session.get(Session, stopped.session)
        assert tool_session is not None and tool_session.status == "stopped"
        snapshot = await session.get(SessionSkillSnapshot, stopped.snapshot)
        assert snapshot is not None and (snapshot.content_retired_at is not None) == retired
        assert snapshot.status == original_snapshot_status
        assert snapshot.retention_released_at == original_release
        task = await session.get(NodeTask, stopped.task)
        assert task is not None and task.status == original_task_status
        assert (
            await session.scalar(
                select(NodeTaskResult).where(NodeTaskResult.node_task_id == stopped.task)
            )
            is None
        )
