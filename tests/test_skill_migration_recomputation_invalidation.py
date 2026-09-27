"""
验证配置选择、安装身份与恢复纪元使旧迁移失效时不能重建可发布旧计划。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from skill_publication_support import new_session
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration_conflicts import counts, pending
from test_skill_migration_recomputation import info
from test_skill_migration_related_sources import linked_sources
from test_skill_migration_resolution_drafts import edit_request, saved_plan, two_conflicts
from test_skill_migration_resolution_service import resolve
from test_skill_preparation import update_version
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute

from agent_remote_server.schemas.skill_library import (
    SkillRemoveRequest,
    SkillRuleRequest,
    SkillScope,
)


@pytest.mark.parametrize("mode", ["forward", "incremental"])
async def test_configuration_change_preserves_explicit_direction_but_supersedes_old_preparation(
    stopped: RuntimeHarness, tmp_path: Path, mode: str
) -> None:
    """
    默认版本变化不能重定向显式迁移，但不再生效的首次准备不能继续替用户发布。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param mode (str): 首次向前准备或独立显式迁移
    """
    original = await pending(stopped, tmp_path, mode)
    assert original.operation_id is not None
    old = await info(stopped, tmp_path, original.operation_id)
    await update_version(stopped, tmp_path, "another default")
    result = await resolve(stopped, tmp_path, original.operation_id, edit_request())
    assert result.status == "superseded"
    if mode == "forward":
        assert result.replacement_id is None and not result.recomputation_possible
        assert "effective_target_changed" in result.stale_reasons
    else:
        assert result.replacement_id is not None and result.recomputation_possible
        replacement = await info(stopped, tmp_path, result.replacement_id)
        assert replacement.target_state_id == old.target_state_id
        assert replacement.incoming.checkpoint_id == old.incoming.checkpoint_id
        assert not replacement.live.recomputation_reasons
        assert await saved_plan(stopped, result.replacement_id) == (0, [], 0)


@pytest.mark.parametrize("reinstall", [False, True])
async def test_removed_or_reinstalled_linked_source_terminates_entire_old_attempt(
    stopped: RuntimeHarness, tmp_path: Path, reinstall: bool
) -> None:
    """
        真正改动的关联来源移除或重装不能被重算替换，也不能只发布仍有效的目标。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param reinstall (bool): 是否恢复同名来源的新安装纪元
    """
    original = await linked_sources(stopped, tmp_path, "head")
    assert original.operation_id is not None
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRemoveRequest(
            skill="notes",
            idempotency_key=str(uuid4()),
            expected_generation=await library.generation(),
        )
    )
    if reinstall:
        await library.add(await library.candidate(name="notes"))
    before = (await command(stopped, tmp_path)).expected
    rows = await counts(stopped)
    result = await resolve(stopped, tmp_path, original.operation_id, edit_request())
    assert result.status == "superseded" and result.replacement_id is None
    assert "related_source_changed" in result.stale_reasons
    assert not result.recomputation_possible and await counts(stopped) == rows
    assert (await command(stopped, tmp_path)).expected == before
    assert (await info(stopped, tmp_path, original.operation_id)).original == original


async def test_restore_invalidates_old_epoch_without_replacement(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    即使恢复到相同字节检查点，新增纪元也禁止旧冲突重新发布。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    identity = await two_conflicts(stopped, tmp_path)
    current = (await command(stopped, tmp_path)).expected.targets[0].head_checkpoint_id
    assert current is not None
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path, checkpoint=current))
    result = await resolve(stopped, tmp_path, identity, edit_request())
    assert result.status == "superseded" and result.stale_reasons == ("state_restore",)
    assert result.replacement_id is None and not result.recomputation_possible


async def test_recomputed_preparation_saves_actual_rule_provenance_before_publication(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    同一版本的规则来源变化仍需保存真实新前置条件，不能配新代数却沿用旧规则标签。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    original = await pending(stopped, tmp_path, "forward")
    assert original.operation_id is not None
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRuleRequest(
            command="enable",
            skill="learning",
            scope=SkillScope(account_id=stopped.account),
            idempotency_key=str(uuid4()),
            expected_generation=await library.generation(),
        )
    )
    expected = (await command(stopped, tmp_path)).expected
    assert expected.targets[0].rule.enabled_source == "account"
    result = await resolve(stopped, tmp_path, original.operation_id, edit_request())
    assert result.replacement_id is not None
    fresh = await info(stopped, tmp_path, result.replacement_id)
    assert fresh.original.before == expected and fresh.status == "conflicted"


@pytest.mark.parametrize("action", ["reset", "restore"])
async def test_unrelated_state_epoch_change_can_recompute_without_reviving_reset_skill(
    stopped: RuntimeHarness, tmp_path: Path, action: str
) -> None:
    """
    独立技能重置只使目录比较过期，未受影响的原输入仍可建立空选择新尝试。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param action (str): 独立技能状态操作
    """
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.add(await library.candidate(name="notes"))
    await new_session(stopped, tmp_path)
    original = await pending(stopped, tmp_path)
    assert original.operation_id is not None
    notes = (await command(stopped, tmp_path, skill="notes")).expected.targets[0]
    await state_execute(
        stopped,
        tmp_path,
        await command(
            stopped,
            tmp_path,
            skill="notes",
            checkpoint=notes.head_checkpoint_id if action == "restore" else None,
        ),
    )
    after = (await command(stopped, tmp_path, skill="notes")).expected
    result = await resolve(stopped, tmp_path, original.operation_id, edit_request())
    assert result.status == "superseded" and result.recomputation_possible
    assert result.replacement_id is not None and "state_" + action in result.stale_reasons
    assert await saved_plan(stopped, result.replacement_id) == (0, [], 0)
    fresh = await info(stopped, tmp_path, result.replacement_id)
    assert not fresh.live.recomputation_reasons and fresh.status == "conflicted"
    assert (await command(stopped, tmp_path, skill="notes")).expected == after
