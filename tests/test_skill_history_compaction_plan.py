"""
验证整理后的完整退役计划保留等待与虚拟成员阻断，并可在同一业务保存点执行。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_directory_compaction import fingerprint, preview, shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_history_planner import preview_history
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.services.skills.compaction import SkillDirectoryCompactionService
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.retention.history import history_retention
from agent_remote_server.services.skills.retention.planning import retirement_plan
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.skill_manager.retention.graph import DirectoryReference, RetentionKey
from agent_remote_server.skill_manager.retention.projection import (
    ProjectedCheckpoint,
    RetentionProjection,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@pytest.mark.parametrize("early", [False, True])
async def test_compaction_history_plan_respects_newly_released_views(
    stopped: RuntimeHarness, tmp_path: Path, early: bool
) -> None:
    """
    普通整理不能立即退役新解除保护的旧视图，明确提前且无全部依赖阻断后才能原子处理。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param early (bool): 是否明确提前结束所有相关历史等待
    """
    old, keep = await shared_directory(stopped, tmp_path)
    async with stopped.database.begin() as session:
        checkpoint = await session.get(SkillCheckpoint, old)
        assert checkpoint is not None
        checkpoint.retention_released_at = datetime.now(UTC) - timedelta(days=31)
    plan = await preview(stopped, tmp_path, old, early=early)
    before = await fingerprint(stopped)
    async with stopped.database.begin() as session:
        projected, history = await SkillDirectoryCompactionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).retirement_preview(stopped.owner, plan)
        assert not session.dirty and not session.new
    assert await fingerprint(stopped) == before
    rows = {row.key: row for row in history.entries}
    keep_key = RetentionKey("checkpoint", str(keep))
    assert keep_key in rows and history.ready == early
    assert not any(identity == keep for _, identity, _ in history.expiring_branches)
    if not early:
        assert "waiting" in rows[keep_key].blockers
        retained_view = rows[keep_key].retention
        assert retained_view is not None
        assert retained_view.released_at == projected.analyzed_at
        return
    keys = tuple(row.key for row in history.entries)
    with pytest.raises(RuntimeError, match="rollback after retirement"):
        async with stopped.database.begin() as session:
            async with retention_mutation(session, stopped.owner):
                await SkillDirectoryCompactionService(
                    session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
                ).apply(stopped.owner, plan)
                await SkillHistoryRetirementService(session, SkillStoragePolicy()).retire(
                    stopped.owner, stopped.account, keys, all_unreferenced=True
                )
                raise RuntimeError("rollback after retirement")
    assert (await fingerprint(stopped))[1:] == before[1:]
    async with stopped.database.begin() as session, retention_mutation(session, stopped.owner):
        actual = await SkillDirectoryCompactionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).apply(stopped.owner, plan)
        assert (
            await SkillHistoryRetirementService(session, SkillStoragePolicy()).retire(
                stopped.owner, stopped.account, keys, all_unreferenced=True
            )
            == keys
        )
    async with stopped.database() as session:
        retained = await session.get(SkillCheckpoint, dict(actual.head_replacements)[keep])
        original = await session.get(SkillCheckpoint, keep)
        assert retained is not None and retained.retained
        assert original is not None and not original.retained


async def test_history_plan_blocks_local_original_without_retiring_revision(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    尚未激活的本地初始版本仍承诺来源内容，显示阻断但不把初始版本变成 state 退役目标。

    :param prepared (RuntimeHarness): 已准备账户
    :param tmp_path (Path): 私有内容卷
    """
    from test_skill_local import register, source

    checkpoint = await source(prepared, tmp_path, linked=True)
    await register(prepared, tmp_path, checkpoint)
    before = await fingerprint(prepared)
    plan = await preview_history(prepared, RetentionKey("checkpoint", str(checkpoint.id)))
    originals = [row for row in plan.entries if row.key.kind == "local_revision"]
    assert not plan.ready and len(originals) == 1
    assert "original_version" in originals[0].blockers
    assert await fingerprint(prepared) == before


async def test_history_plan_blocks_replacement_member_retaining_selected_history(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    虚拟新目录仍引用被选旧成员时必须阻断，不能反向把尚未发布的新目录也选为可删历史。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    virtual = uuid4()
    async with stopped.database() as session:
        index = await SkillRetentionRepository(session).load(stopped.owner)
        checkpoint = next(row for row in index.checkpoints if row.id == old)
        assert checkpoint.state_id is not None and checkpoint.backing_directory_id is not None
        projection = RetentionProjection(
            branch_heads=(),
            directory_heads=((stopped.account, virtual),),
            checkpoints=(
                ProjectedCheckpoint(
                    virtual, checkpoint.backing_directory_id, None, checkpoint.content_digest, None
                ),
            ),
            members=(
                DirectoryReference(
                    stopped.account, virtual, checkpoint.subtree_prefix, checkpoint.state_id, old
                ),
            ),
            tree_objects=(),
        )
        now = datetime.now(UTC)
        plan = retirement_plan(
            index,
            stopped.account,
            (RetentionKey("checkpoint", str(old)),),
            history_retention(index, protection(index, now, projection), SkillStoragePolicy()),
            frozenset(),
            now,
            all_unreferenced=True,
            projection=projection,
        )
        assert not plan.ready
        blocker = next(row for row in plan.entries if row.key.identity == str(virtual))
        assert blocker.blockers == ("replacement_reference",) and blocker.retention is None
        assert UUID(blocker.key.identity) not in {row.id for row in index.checkpoints}
