"""
验证真实账户/单项候选的只读范围、完整恢复损失、普通整理等待与实际结算。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_compaction_reclamation import persisted_content
from test_skill_content_gc import upload
from test_skill_content_service import database as database
from test_skill_directory_compaction import shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.models.skill_storage import SkillStoredTree
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_state_commands import SkillStateSelector
from agent_remote_server.services.skills.prune import SkillPruneService
from agent_remote_server.services.skills.prune.plan import PrunePlan, PruneResult
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.clocks import clock_records
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def candidate_plan(
    state: RuntimeHarness,
    root: Path,
    *,
    skill: str | None = None,
    early: bool = True,
    cutoff: datetime | None = None,
) -> PrunePlan:
    """
    使用独立提交的预览事务，并验证全用户业务列值及 ORM 状态完全不变。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 内容卷
    :param skill (str | None): 单项来源，空值表示完整目录
    :param early (bool): 是否明确提前结束等待
    :param cutoff (datetime | None): 原始固定截止
    :return PrunePlan: 完整候选与动作
    """
    before = await persisted_content(state)
    async with state.database.begin() as session:
        result = await SkillPruneService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).preview(
            state.owner,
            SkillStateSelector(
                account_id=state.account,
                scope="item" if skill else "account-directory",
                skill=skill,
            ),
            all_unreferenced=early,
            cutoff=cutoff,
        )
        assert not session.new and not session.dirty and not session.deleted
    assert await persisted_content(state) == before
    return result


async def execute_plan(state: RuntimeHarness, root: Path, plan: PrunePlan) -> PruneResult:
    """
    重新实例化服务并使用原确认计划执行，最终提交权保留在调用事务。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 私有卷
    :param plan (PrunePlan): 原始预览
    :return PruneResult: 已提交完整结果
    """
    async with state.database.begin() as session:
        return await SkillPruneService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).apply(state.owner, plan)


async def age_unprotected(state: RuntimeHarness) -> None:
    """
    为测试构造已确证的旧释放事件，不把保护中的时钟伪造为已过期。

    :param state (RuntimeHarness): 原始账户
    """
    async with state.database.begin() as session:
        index = await SkillRetentionRepository(session).load(state.owner)
        protected = protection(index, datetime.now(UTC))
        for key, row in clock_records(index).items():
            if key not in protected.protected:
                row.retention_released_at = datetime.now(UTC) - timedelta(days=100)


@pytest.mark.parametrize("skill", [None, "learning"])
async def test_scope_forecast_executes_whole_groups_and_preserves_unbound_content(
    stopped: RuntimeHarness, tmp_path: Path, skill: str | None
) -> None:
    """
    账户及单项均列出完整连带历史，新等价头保留原内容，未绑定上传不进入范围。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param skill (str | None): 目录或单项范围
    """
    old, keep = await shared_directory(stopped, tmp_path)
    _, unbound, _ = await upload(stopped.database, tmp_path, stopped.owner, data=b"unbound")
    plan = await candidate_plan(stopped, tmp_path, skill=skill)
    assert plan.retirement is not None and plan.retirement.ready
    keys = {row.key for row in plan.retirement.entries if row.retained}
    assert RetentionKey("checkpoint", str(old)) in keys
    assert RetentionKey("checkpoint", str(keep)) in keys
    assert {key for group in plan.selection.groups for key in group} == keys
    assert unbound not in plan.content.requested_trees
    assert plan.content.state_bytes > 0
    if skill:
        async with stopped.database() as session:
            index = await SkillRetentionRepository(session).load(stopped.owner)
            branch_ids = {
                row.id for row in index.branches if row.installation_id == plan.scope.source_id
            }
            initial = set(plan.candidates.requested) if plan.candidates else set()
            assert RetentionKey("checkpoint", str(keep)) not in initial
            assert all(
                row.state_id in branch_ids
                for row in index.checkpoints
                if RetentionKey("checkpoint", str(row.id)) in initial
            )
    result = await execute_plan(stopped, tmp_path, plan)
    assert set(result.retired) == keys
    assert result.content.state_bytes == plan.content.state_bytes
    assert result.content.pending_file_bytes == plan.content.pending_file_bytes
    async with stopped.database() as session:
        assert await session.get(SkillStoredTree, (stopped.owner, "state", unbound.identity))
        assert result.compaction is not None
        replacement = await session.get(
            SkillCheckpoint, dict(result.compaction.head_replacements)[keep]
        )
        assert replacement is not None and replacement.retained


async def test_ordinary_plan_compacts_now_and_retires_only_after_new_wait(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    普通模式把整理和等待中的组明确分开，整理后新时钟到期才可选入完整退役。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    """
    old, keep = await shared_directory(stopped, tmp_path)
    await age_unprotected(stopped)
    plan = await candidate_plan(stopped, tmp_path, early=False)
    assert plan.compaction is not None and plan.compaction.changed_directories()
    key = RetentionKey("checkpoint", str(old))
    assert key in plan.selection.blocked
    assert not plan.retirement or key not in {row.key for row in plan.retirement.entries}
    result = await execute_plan(stopped, tmp_path, plan)
    assert key not in result.retired and result.compaction is not None
    async with stopped.database() as session:
        old_keep = await session.get(SkillCheckpoint, keep)
        assert (
            old_keep is not None
            and old_keep.retained
            and old_keep.retention_released_at is not None
        )
    await age_unprotected(stopped)
    later = await candidate_plan(stopped, tmp_path, early=False)
    assert later.retirement is not None and key in {row.key for row in later.retirement.entries}
    retired = await execute_plan(stopped, tmp_path, later)
    assert key in retired.retired and retired.content.state_bytes > 0


async def test_persisted_claim_can_sweep_previously_waiting_tree(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    历史到期而无保护树刚被重新上传时先退役历史，后续只据实际内容生命周期归属清理到期树。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    await shared_directory(stopped, tmp_path)
    await age_unprotected(stopped)
    initial = await candidate_plan(stopped, tmp_path, early=False)
    await execute_plan(stopped, tmp_path, initial)
    await age_unprotected(stopped)
    async with stopped.database.begin() as session:
        rows = await session.scalars(
            select(SkillStoredTree).where(
                SkillStoredTree.user_id == stopped.owner, SkillStoredTree.category == "state"
            )
        )
        for row in rows:
            row.retention_released_at = datetime.now(UTC)
    history_only = await candidate_plan(stopped, tmp_path, early=False)
    assert history_only.retirement is not None
    assert not history_only.content.requested_trees
    result = await execute_plan(stopped, tmp_path, history_only)
    assert result.retired and result.content.state_bytes == 0
    await age_unprotected(stopped)
    sweep = await candidate_plan(stopped, tmp_path, early=False)
    assert sweep.content.requested_trees and sweep.content.state_bytes > 0
    result = await execute_plan(stopped, tmp_path, sweep)
    assert not result.retired and result.content.state_bytes == sweep.content.state_bytes
