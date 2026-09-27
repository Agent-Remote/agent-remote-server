"""
验证成功替代不跨越迁移方向与状态纪元，旧失效输入不能取得新成功身份。
"""

from pathlib import Path

import pytest
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_migration import migrate, request
from test_skill_migration_conflicts import pending
from test_skill_migration_recomputation import info
from test_skill_migration_resolution_drafts import edit_request
from test_skill_migration_resolution_service import resolve
from test_skill_preparation import update_version
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute

from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice


@pytest.mark.parametrize("directory", [False, True])
async def test_new_epoch_success_does_not_replace_old_epoch_attempt(
    stopped: RuntimeHarness, tmp_path: Path, directory: bool
) -> None:
    """
    新纪元命令可以成功，但不能作为原纪元计划继续发布的替代比较。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param directory (bool): 是否重置完整目录纪元
    """
    original = await pending(stopped, tmp_path)
    assert isinstance(original, SkillMigrationView) and original.operation_id is not None
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path, directory=directory))
    before = original.before
    newer = await migrate(
        stopped,
        tmp_path,
        await request(stopped, tmp_path, before.source.revision_id, before.target.revision_id),
    )
    assert newer.operation_id is not None
    if newer.status == "conflicted":
        result = await resolve(
            stopped,
            tmp_path,
            newer.operation_id,
            edit_request(SkillResolutionChoice(use="current")),
        )
        assert result.status == "published"
    else:
        assert newer.status == "ready"
    old = await info(stopped, tmp_path, original.operation_id)
    assert old.status == "superseded" and old.replacement_id is None
    assert old.superseded_reason == "state_reset" and old.original == original


async def test_other_target_success_does_not_supersede_explicit_pending_direction(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    同一源向另一版本成功迁移不能夺取仍独立有效的原显式方向。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    original = await pending(stopped, tmp_path)
    assert isinstance(original, SkillMigrationView) and original.operation_id is not None
    another_target = await update_version(stopped, tmp_path, "third version")
    newer = await migrate(
        stopped,
        tmp_path,
        await request(stopped, tmp_path, original.before.source.revision_id, another_target),
    )
    assert newer.status == "conflicted" and newer.operation_id is not None
    result = await resolve(
        stopped, tmp_path, newer.operation_id, edit_request(SkillResolutionChoice(use="current"))
    )
    assert result.status == "published"
    old = await info(stopped, tmp_path, original.operation_id)
    assert old.status == "conflicted" and old.replacement_id is None and old.original == original
