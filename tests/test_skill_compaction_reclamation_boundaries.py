"""
验证精确组合的剩余引用、完整大选择与确认期间变化，不把零释放误认为可扩大范围。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from skill_runtime_support import RuntimeHarness
from test_skill_compaction_reclamation import complete_selection, forecast
from test_skill_content_service import database as database
from test_skill_directory_compaction import fingerprint, preview, shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_history_planner import add_backed_history
from test_skill_preparation import pin
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_state import AccountSkillState, SkillCheckpoint
from agent_remote_server.services.skills.compaction import SkillDirectoryCompactionService
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def test_unselected_original_inputs_keep_tree_and_zero_release_composition(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    仅选择发布后旧 head 时原上传输入仍保留，不隐式扩张到同摘要消费者。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    predicted = await forecast(stopped, tmp_path, await preview(stopped, tmp_path, old))
    assert predicted.ready and predicted.content is not None
    assert not predicted.content.requested_trees and predicted.content.state_bytes == 0
    assert any(tree.references for tree in predicted.trees)
    async with stopped.database.begin() as session:
        result = await SkillDirectoryCompactionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).apply_reclamation(stopped.owner, predicted)
    assert result.compaction.head_replacements and result.retired
    assert result.content.state_bytes == 0 and not result.content.deletion_ids


async def test_large_exact_selection_is_not_split_and_outer_rollback_is_complete(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    超过一千项的显式整理与完整退役共用一次保存点，最后失败后所有原始身份可恢复。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    added = await add_backed_history(stopped, old, 1001)
    plan = await complete_selection(stopped, tmp_path, old)
    assert len(plan.checkpoint_ids) > 1000 and set(added) <= set(plan.checkpoint_ids)
    predicted = await forecast(stopped, tmp_path, plan)
    assert predicted.ready and predicted.content is not None and predicted.content.state_bytes > 0
    before = await fingerprint(stopped)
    with pytest.raises(RuntimeError, match="complete outer rollback"):
        async with stopped.database.begin() as session:
            await SkillDirectoryCompactionService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).apply_reclamation(stopped.owner, predicted)
            raise RuntimeError("complete outer rollback")
    assert (await fingerprint(stopped))[1:] == before[1:]
    async with stopped.database.begin() as session:
        result = await SkillDirectoryCompactionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).apply_reclamation(stopped.owner, predicted)
    assert {RetentionKey("checkpoint", str(identity)) for identity in added} <= set(result.retired)
    assert result.content.state_bytes == predicted.content.state_bytes


@pytest.mark.parametrize("change", ["pin", "consumer"])
async def test_changed_pin_or_consumer_invalidates_original_composition(
    stopped: RuntimeHarness, tmp_path: Path, change: str
) -> None:
    """
    保护和完整历史闭包任一变化都在整理前拒绝，不能只重验原选择中的行。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param change (str): 新 pin 或新保留消费者
    """
    old, _ = await shared_directory(stopped, tmp_path)
    predicted = await forecast(stopped, tmp_path, await complete_selection(stopped, tmp_path, old))
    if change == "consumer":
        await add_backed_history(stopped, old, 1)
    else:
        async with stopped.database() as session:
            checkpoint = await session.get(SkillCheckpoint, old)
            assert checkpoint is not None
            branch = await session.get(AccountSkillState, checkpoint.state_id)
            assert branch is not None and branch.base_revision_id is not None
            revision = branch.base_revision_id
        await pin(stopped, tmp_path, revision)
    before = await fingerprint(stopped)
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillDirectoryCompactionService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).apply_reclamation(stopped.owner, predicted)
        assert error.value.code in {"STATE_PROTECTED", "HEAD_CHANGED"}
    assert (await fingerprint(stopped))[1:] == before[1:]


@pytest.mark.parametrize("future", [False, True])
async def test_analysis_time_must_be_aware_and_not_in_future(
    stopped: RuntimeHarness, tmp_path: Path, future: bool
) -> None:
    """
    明确时间不得伪造已过期资格，非法时间在生成任何预测前拒绝。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param future (bool): 是否使用未来时间而不是无时区时间
    """
    old, _ = await shared_directory(stopped, tmp_path)
    plan = await preview(stopped, tmp_path, old)
    now = datetime.now(UTC)
    invalid = now + timedelta(days=1) if future else now.replace(tzinfo=None)
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillDirectoryCompactionService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).reclamation_preview(stopped.owner, plan, analyzed_at=invalid)
        assert error.value.code == "INVALID_REQUEST"
        assert not session.dirty and not session.new
