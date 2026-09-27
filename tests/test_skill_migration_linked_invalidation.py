"""
验证关联但未修改的成员不阻止新比较，历史整侧导入仍受精确身份纪元约束。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration_recomputation import info
from test_skill_migration_related_sources import linked_sources
from test_skill_migration_resolution_drafts import edit, edit_request, saved_plan
from test_skill_migration_resolution_service import resolve
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute

from agent_remote_server.models.skill_state import AccountSkillState, SkillDirectoryMember
from agent_remote_server.schemas.skill_library import SkillRemoveRequest, SkillUpdateRequest
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.content import SkillContentError


async def change_related(state: RuntimeHarness, root: Path, action: str) -> None:
    """
    在旧比较之后通过真实生命周期命令改变关联身份或纪元。

    :param state (RuntimeHarness): 原账户
    :param root (Path): 私有内容卷
    :param action (str): 重置、恢复、移除、重装或切换版本
    """
    library = LibraryHarness(state.database, root, state.owner)
    if action in {"remove", "reinstall"}:
        await library.execute(
            SkillRemoveRequest(
                skill="notes",
                idempotency_key=str(uuid4()),
                expected_generation=await library.generation(),
            )
        )
        if action == "remove":
            return
        await library.add(await library.candidate(name="notes"))
    if action == "revision":
        await library.execute(
            SkillUpdateRequest(
                skill="notes",
                item=await library.candidate(name="notes", version="two"),
                idempotency_key=str(uuid4()),
                expected_generation=await library.generation(),
            )
        )
    checkpoint = None
    if action == "restore":
        checkpoint = (
            (await command(state, root, skill="notes")).expected.targets[0].head_checkpoint_id
        )
        assert checkpoint is not None
    await state_execute(
        state, root, await command(state, root, skill="notes", checkpoint=checkpoint)
    )


@pytest.mark.parametrize("action", ["reset", "restore", "remove", "reinstall", "revision"])
async def test_untouched_linked_member_keeps_current_identity_and_rejects_old_incoming(
    stopped: RuntimeHarness, tmp_path: Path, action: str
) -> None:
    """
    新比较的当前选择不写关联分支，但完整旧侧绝不能借相同字节越过真实生命周期。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param action (str): 关联成员的生命周期变化
    """
    original = await linked_sources(stopped, tmp_path, "none")
    assert original.operation_id is not None
    await change_related(stopped, tmp_path, action)
    result = await resolve(stopped, tmp_path, original.operation_id, edit_request())
    replacement = result.replacement_id
    assert result.status == "superseded" and result.recomputation_possible and replacement
    assert await saved_plan(stopped, replacement) == (0, [], 0)
    fresh = await info(stopped, tmp_path, replacement)
    assert fresh.directory.checkpoint_id is not None
    async with stopped.database() as session:
        member = await session.get(SkillDirectoryMember, (fresh.directory.checkpoint_id, "notes"))
        assert member is not None
        branch = await session.get(AccountSkillState, member.state_id)
        assert branch is not None
        unchanged = (member.state_id, member.checkpoint_id, branch.head_checkpoint_id, branch.epoch)
    with pytest.raises(SkillContentError) as error:
        await resolve(stopped, tmp_path, replacement, edit_request())
    assert error.value.code in {"STATE_EPOCH_CHANGED", "SKILL_SOURCE_CONFLICT", "SOURCE_CHANGED"}
    assert await saved_plan(stopped, replacement) == (0, [], 0)
    published = await resolve(
        stopped, tmp_path, replacement, edit_request(SkillResolutionChoice(use="current"))
    )
    assert published.status == "published" and [item.name for item in published.affected] == [
        "learning"
    ]
    async with stopped.database() as session:
        member = await session.get(SkillDirectoryMember, (published.result_directory_id, "notes"))
        assert member is not None
        branch = await session.get(AccountSkillState, member.state_id)
        assert branch is not None
        assert (
            member.state_id,
            member.checkpoint_id,
            branch.head_checkpoint_id,
            branch.epoch,
        ) == unchanged
    assert (await info(stopped, tmp_path, original.operation_id)).original == original


@pytest.mark.parametrize("action", ["reset", "restore", "revision"])
@pytest.mark.parametrize("saved_current", [False, True])
async def test_only_the_retained_candidates_actual_writes_bind_related_epochs(
    stopped: RuntimeHarness, tmp_path: Path, action: str, saved_current: bool
) -> None:
    """
    已保存当前侧明确不改关联项；尚未选择的旧输入确实改动关联项时整次失效。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param action (str): 关联成员变化
    :param saved_current (bool): 是否已明确保存保留关联当前侧的完整计划
    """
    original = await linked_sources(stopped, tmp_path, "head")
    assert original.operation_id is not None
    if saved_current:
        await edit(
            stopped,
            tmp_path,
            original.operation_id,
            edit_request(SkillResolutionChoice(use="current")),
        )
    old_plan = await saved_plan(stopped, original.operation_id)
    await change_related(stopped, tmp_path, action)
    before = (await command(stopped, tmp_path, skill="notes")).expected
    result = await resolve(
        stopped, tmp_path, original.operation_id, edit_request(revision=int(saved_current))
    )
    assert result.status == "superseded" and result.recomputation_possible == saved_current
    assert (result.replacement_id is not None) == saved_current
    assert (await command(stopped, tmp_path, skill="notes")).expected == before
    assert (await saved_plan(stopped, original.operation_id))[:2] == old_plan[:2]
    if result.replacement_id is not None:
        assert await saved_plan(stopped, result.replacement_id) == (0, [], 0)
