"""
验证目录整理对活动输入、旧 provenance、归档纪元与无效格式的保守边界。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from skill_publication_support import new_session
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_directory_compaction import apply, preview, shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_preparation import pin
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_state import AccountSkillState, SkillCheckpoint
from agent_remote_server.schemas.skill_library import SkillRemoveRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def test_compaction_never_rewrites_active_snapshot_inputs(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    重新选择旧版创建的实际快照在取消 pin 后仍保活原目录，显式提前也不能移除其成员。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    async with stopped.database() as session:
        checkpoint = await session.get(SkillCheckpoint, old)
        assert checkpoint is not None
        branch = await session.get(AccountSkillState, checkpoint.state_id)
        assert branch is not None and branch.base_revision_id is not None
        revision = branch.base_revision_id
    await pin(stopped, tmp_path, revision)
    await new_session(stopped, tmp_path)
    await pin(stopped, tmp_path, None)
    with pytest.raises(SkillContentError) as error:
        await preview(stopped, tmp_path, old)
    assert error.value.code == "STATE_PROTECTED"


async def test_compaction_reports_legacy_backing_without_guessing_its_roots(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    旧 head 缺少 backing 时保持原身份和完整树，即使当前目录另有可整理的成员。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, keep = await shared_directory(stopped, tmp_path)
    async with stopped.database.begin() as session:
        checkpoint = await session.get(SkillCheckpoint, keep)
        assert checkpoint is not None
        checkpoint.backing_directory_id = None
        digest = checkpoint.tree_digest
    plan = await preview(stopped, tmp_path, old)
    assert any(
        head.checkpoint_id == keep and head.backing_directory_id is None for head in plan.heads
    )
    result = await apply(stopped, tmp_path, plan)
    assert result.directory_replacements and not result.head_replacements
    async with stopped.database() as session:
        checkpoint = await session.get(SkillCheckpoint, keep)
        assert checkpoint is not None and checkpoint.retained and checkpoint.tree_digest == digest
        branch = await session.get(AccountSkillState, checkpoint.state_id)
        assert branch is not None and branch.head_checkpoint_id == keep


async def test_compaction_preserves_invalid_skill_files_and_flags(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    无效格式仍是用户学习状态，整理不得替换成原始包或清空诊断。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, keep = await shared_directory(stopped, tmp_path, invalid=True)
    async with stopped.database() as session:
        checkpoint = await session.get(SkillCheckpoint, keep)
        assert checkpoint is not None and checkpoint.invalid_skill_format
        queries = SkillStateQueryService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        original = await queries.tree(stopped.owner, keep)
    plan = await preview(stopped, tmp_path, old)
    assert any(row.invalid_skill_format for row in plan.directories)
    result = await apply(stopped, tmp_path, plan)
    async with stopped.database() as session:
        identity = dict(result.head_replacements)[keep]
        checkpoint = await session.get(SkillCheckpoint, identity)
        assert checkpoint is not None and checkpoint.invalid_skill_format
        queries = SkillStateQueryService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        assert (await queries.tree(stopped.owner, identity)).manifest == original.manifest


async def test_compaction_uses_archive_wait_after_same_source_reinstall(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    同来源重新安装不把旧纪元历史变成普通三十天候选，真实九十天期限仍适用。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRemoveRequest(
            skill="learning",
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    await library.add(await library.candidate())
    for days in (31, 91):
        async with stopped.database.begin() as session:
            checkpoint = await session.get(SkillCheckpoint, old)
            assert checkpoint is not None
            checkpoint.retention_released_at = datetime.now(UTC) - timedelta(days=days)
        if days == 31:
            with pytest.raises(SkillContentError) as error:
                await preview(stopped, tmp_path, old, early=False)
            assert error.value.code == "HISTORY_NOT_EXPIRED"
        else:
            assert (await preview(stopped, tmp_path, old, early=False)).changed_directories()
