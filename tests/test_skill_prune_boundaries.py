"""
验证候选范围、截止、完整组阻断和最后故障回滚，保护不能因存在其他可删组被绕过。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_compaction_reclamation import persisted_content
from test_skill_content_service import database as database
from test_skill_directory_compaction import fingerprint, shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_history_planner import add_backed_history
from test_skill_local import register, source
from test_skill_prune_candidates import candidate_plan, execute_plan
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.models.skill_local import AccountLocalSkillRevision
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.prune import SkillPruneService
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def test_original_local_dependency_blocks_its_group_but_not_independent_directory(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    本地初始版本使其来源组不可删；同账户独立完整目录可在预览中明确选择，不需要整理。

    :param prepared (RuntimeHarness): 已管理账户
    :param tmp_path (Path): 内容卷
    """
    original = await source(prepared, tmp_path, linked=True)
    local = await register(prepared, tmp_path, original)
    free = await source(prepared, tmp_path, name="other", content=b"independent directory")
    plan = await candidate_plan(prepared, tmp_path)
    assert plan.compaction is None
    blocked = RetentionKey("checkpoint", str(original.id))
    chosen = RetentionKey("checkpoint", str(free.id))
    assert blocked in plan.selection.blocked and chosen not in plan.selection.blocked
    assert plan.candidates is not None
    assert any("original_version" in row.blockers for row in plan.candidates.entries)
    result = await execute_plan(prepared, tmp_path, plan)
    assert chosen in result.retired and blocked not in result.retired and result.compaction is None
    async with prepared.database() as session:
        initial = await session.get(AccountLocalSkillRevision, local.default_revision_id)
        retained = await session.get(SkillCheckpoint, original.id)
        assert (
            initial is not None and initial.retained and retained is not None and retained.retained
        )


async def test_late_consumer_beyond_cutoff_still_invalidates_original_plan(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    截止外新消费者不进入初始根，但完整依赖仍必须看见它并拒绝旧确认。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    original = await candidate_plan(stopped, tmp_path)
    added = (await add_backed_history(stopped, old, 1))[0]
    async with stopped.database.begin() as session:
        row = await session.get(SkillCheckpoint, added)
        assert row is not None
        row.created_at = original.cutoff + timedelta(seconds=1)
    updated = await candidate_plan(stopped, tmp_path, cutoff=original.cutoff)
    key = RetentionKey("checkpoint", str(added))
    assert updated.candidates is not None and key not in updated.candidates.requested
    assert key in {row.key for row in updated.candidates.entries}
    before = await fingerprint(stopped)
    with pytest.raises(SkillContentError) as error:
        await execute_plan(stopped, tmp_path, original)
    assert error.value.code == "HEAD_CHANGED"
    assert (await fingerprint(stopped))[1:] == before[1:]


async def test_candidate_last_flush_failure_rolls_back_all_selected_groups(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    多组与整理一起执行至真实任务写入后失败，捕获并提交仍完整撤销全部内容变化。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    :param monkeypatch (pytest.MonkeyPatch): 最后故障注入
    """
    await shared_directory(stopped, tmp_path)
    await source(stopped, tmp_path, name="independent", content=b"second group")
    plan = await candidate_plan(stopped, tmp_path)
    assert len(plan.selection.groups) > 1 and plan.content.pending_file_bytes > 0
    before = await persisted_content(stopped)
    original = SkillContentGCRepository.flush
    calls = 0

    async def fail(repository: SkillContentGCRepository) -> None:
        """
        让树与持久化任务真正 flush 后才抛错，覆盖最晚回滚边界。

        :param repository (SkillContentGCRepository): 同事务仓储
        """
        nonlocal calls
        await original(repository)
        calls += 1
        if calls == 2:
            raise RuntimeError("candidate last flush")

    monkeypatch.setattr(SkillContentGCRepository, "flush", fail)
    async with stopped.database.begin() as session:
        with pytest.raises(RuntimeError, match="candidate last flush"):
            await SkillPruneService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).apply(stopped.owner, plan)
    assert calls == 2
    after = await persisted_content(stopped)
    assert tuple(row for row in before if row[0] != "skill_storage_usage") == tuple(
        row for row in after if row[0] != "skill_storage_usage"
    )
    async with stopped.database() as session:
        assert not list(
            await session.scalars(
                select(SkillContentDeletion).where(SkillContentDeletion.user_id == stopped.owner)
            )
        )


async def test_full_account_candidate_groups_include_more_than_one_thousand_consumers(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    候选扫描和消费者传播都不截断旧的一千项边界，成功执行包含所有明确损失。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    added = await add_backed_history(stopped, old, 1001)
    plan = await candidate_plan(stopped, tmp_path, skill="learning")
    assert any(len(group) > 1000 for group in plan.selection.groups)
    result = await execute_plan(stopped, tmp_path, plan)
    assert {RetentionKey("checkpoint", str(identity)) for identity in added} <= set(result.retired)


@pytest.mark.parametrize("future", [False, True])
async def test_invalid_cutoff_and_foreign_owner_cannot_execute_candidates(
    stopped: RuntimeHarness, tmp_path: Path, future: bool
) -> None:
    """
    无时区或未来截止不能伪造到期资格，其他用户也不能提交完整合法原计划。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param future (bool): 未来或无时区截止
    """
    await shared_directory(stopped, tmp_path)
    now = datetime.now(UTC)
    invalid = now + timedelta(days=1) if future else now.replace(tzinfo=None)
    with pytest.raises(SkillContentError) as error:
        await candidate_plan(stopped, tmp_path, cutoff=invalid)
    assert error.value.code == "INVALID_REQUEST"
    plan = await candidate_plan(stopped, tmp_path)
    before = await persisted_content(stopped)
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillPruneService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).apply(uuid4(), plan)
        assert error.value.code == "ACCOUNT_NOT_FOUND"
    assert await persisted_content(stopped) == before
