"""
验证配置与成功发布立即使精确范围的旧迁移失效，原输入和选择始终保留。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration import migrate, request
from test_skill_migration_conflicts import pending
from test_skill_migration_recomputation import info
from test_skill_migration_resolution_drafts import edit, edit_request, saved_plan, two_conflicts
from test_skill_migration_resolution_service import resolve
from test_skill_preparation import pin, prepare, update_version
from test_skill_preparation import request as preparation_request
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute

from agent_remote_server.schemas.skill_library import (
    SkillRemoveRequest,
    SkillRuleRequest,
    SkillScope,
)
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.tool_registry import ToolRegistry, ToolRuntimeTemplate


@pytest.mark.parametrize("mode", ["forward", "incremental"])
async def test_removal_immediately_supersedes_target_and_reinstallation_does_not_reactivate_it(
    stopped: RuntimeHarness, tmp_path: Path, mode: str
) -> None:
    """
    无需再次 resolve 即可看见移除失效，重装及历史回执重放不恢复旧计划。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param mode (str): 首次准备或显式迁移
    """
    original = await pending(stopped, tmp_path, mode)
    assert original.operation_id is not None
    await edit(stopped, tmp_path, original.operation_id, edit_request())
    plan = await saved_plan(stopped, original.operation_id)
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    command = SkillRemoveRequest(
        skill="learning",
        idempotency_key=str(uuid4()),
        expected_generation=await library.generation(),
    )
    removed = await library.execute(command)
    invalidated = await info(stopped, tmp_path, original.operation_id)
    assert (
        invalidated.status == "superseded"
        and invalidated.superseded_reason == "installation_removed"
    )
    assert invalidated.replacement_id is None and invalidated.original == original
    await library.add(await library.candidate())
    assert await library.execute(command) == removed
    assert (await info(stopped, tmp_path, original.operation_id)).status == "superseded"
    assert await saved_plan(stopped, original.operation_id) == plan


@pytest.mark.parametrize("mode", ["forward", "incremental"])
@pytest.mark.parametrize("scope", ["account", "other_account", "tool", "other_tool"])
async def test_rule_invalidation_obeys_actual_account_selection_and_explicit_direction(
    stopped: RuntimeHarness, tmp_path: Path, mode: str, scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    仅当前账户生效目标改变会取消准备，显式迁移方向与启用规则相互独立。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param mode (str): 首次或显式模式
    :param scope (str): 实际或独立账户与工具范围
    :param monkeypatch (pytest.MonkeyPatch): 独立工具测试注册表
    """
    original = await pending(stopped, tmp_path, mode)
    assert original.operation_id is not None
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    monkeypatch.setitem(
        ToolRegistry._templates,
        "test-tool",
        ToolRuntimeTemplate("test-tool", "test", ["test"], "test", "test"),
    )
    selected = (
        SkillScope(account_id=stopped.account if scope == "account" else await library.account())
        if scope in {"account", "other_account"}
        else SkillScope(tools=("claude" if scope == "tool" else "test-tool",))
    )
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="learning",
            scope=selected,
            idempotency_key=str(uuid4()),
            expected_generation=await library.generation(),
        )
    )
    current = await info(stopped, tmp_path, original.operation_id)
    invalid = mode == "forward" and scope in {"account", "tool"}
    assert current.status == ("superseded" if invalid else "conflicted")
    assert current.superseded_reason == ("effective_target_changed" if invalid else None)
    assert current.original == original


async def test_pinned_preparation_survives_default_revision_change(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    库默认变化不等于各账户有效版本变化，固定原目标的准备仍有效。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    original = await pending(stopped, tmp_path, "forward")
    assert original.operation_id is not None
    target = (await info(stopped, tmp_path, original.operation_id)).live.target.revision_id
    await pin(stopped, tmp_path, target)
    await update_version(stopped, tmp_path, "independent default")
    assert (await info(stopped, tmp_path, original.operation_id)).status == "conflicted"


@pytest.mark.parametrize("writer", ["resolve", "migrate", "recompute"])
async def test_success_proactively_replaces_other_pending_comparisons(
    stopped: RuntimeHarness, tmp_path: Path, writer: str
) -> None:
    """
    每条成功写入路径都在返回前取消同方向旧比较，重算父记录仍由自身 CAS 设置。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param writer (str): 人工解决、显式迁移或干净重算
    """
    identity = await two_conflicts(stopped, tmp_path)
    original = await info(stopped, tmp_path, identity)
    assert isinstance(original.original, SkillMigrationView)
    before = original.original.before
    second = await migrate(
        stopped,
        tmp_path,
        await request(stopped, tmp_path, before.source.revision_id, before.target.revision_id),
    )
    assert second.operation_id is not None and second.status == "conflicted"
    await edit(
        stopped,
        tmp_path,
        identity,
        edit_request(SkillResolutionChoice(path="learning/a", use="incoming")),
    )
    old_plan = await saved_plan(stopped, identity)
    if writer != "resolve":
        current = await new_session(stopped, tmp_path)
        await publish(
            current,
            tmp_path,
            await ingest(current, tmp_path, {"learning/a": b"theirs", "learning/b": b"theirs"}),
        )
    if writer == "migrate":
        success = await migrate(
            stopped,
            tmp_path,
            await request(stopped, tmp_path, before.source.revision_id, before.target.revision_id),
        )
        assert success.status == "ready"
        success_id = success.operation_id
    else:
        result = await resolve(
            stopped,
            tmp_path,
            second.operation_id,
            edit_request(SkillResolutionChoice(use="current")),
        )
        assert result.status == ("published" if writer == "resolve" else "superseded")
        success_id = second.operation_id if writer == "resolve" else result.replacement_id
    assert success_id is not None
    old = await info(stopped, tmp_path, identity)
    assert old.status == "superseded" and old.replacement_id == success_id
    assert (
        old.superseded_reason == "migration_baseline_changed" and old.original == original.original
    )
    assert await saved_plan(stopped, identity) == old_plan
    if writer == "recompute":
        parent = await info(stopped, tmp_path, second.operation_id)
        assert parent.status == "superseded" and parent.replacement_id == success_id
        assert (await info(stopped, tmp_path, success_id)).recomputed_from_id == second.operation_id


async def test_successful_forward_preparation_supersedes_earlier_preparation_conflict(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    首次准备的成功基线也立即取代旧比较，且旧来源检查点仍保持原样。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/SKILL.md": b"local edit"})
    )
    late = await new_session(stopped, tmp_path)
    await update_version(stopped, tmp_path, "new upstream")
    original = await prepare(stopped, tmp_path, await preparation_request(stopped, tmp_path))
    assert original.status == "conflicted" and original.operation_id is not None
    matching = "---\nname: learning\ndescription: 学习\n---\nnew upstream\n".encode()
    await publish(late, tmp_path, await ingest(late, tmp_path, {"learning/SKILL.md": matching}))
    success = await prepare(stopped, tmp_path, await preparation_request(stopped, tmp_path))
    assert (
        success.status == "ready" and success.mode == "forward" and success.migration_sequence == 1
    )
    old = await info(stopped, tmp_path, original.operation_id)
    assert old.status == "superseded" and old.replacement_id == success.operation_id
    assert old.original == original


@pytest.mark.parametrize("action", ["reset", "restore"])
async def test_later_success_links_previously_cancelled_same_epoch_attempt(
    stopped: RuntimeHarness, tmp_path: Path, action: str
) -> None:
    """
    独立项取消旧计划后，同方向成功记录仍能成为其替代，已有替代关系不会被覆盖。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param action (str): 独立项的状态变化
    """
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.add(await library.candidate(name="notes"))
    await new_session(stopped, tmp_path)
    original = await pending(stopped, tmp_path)
    assert isinstance(original, SkillMigrationView) and original.operation_id is not None
    checkpoint = (
        (await command(stopped, tmp_path, skill="notes")).expected.targets[0].head_checkpoint_id
    )
    assert checkpoint is not None
    await state_execute(
        stopped,
        tmp_path,
        await command(
            stopped, tmp_path, skill="notes", checkpoint=checkpoint if action == "restore" else None
        ),
    )
    cancelled = await info(stopped, tmp_path, original.operation_id)
    assert cancelled.status == "superseded" and cancelled.replacement_id is None
    before = original.before
    newer = await migrate(
        stopped,
        tmp_path,
        await request(stopped, tmp_path, before.source.revision_id, before.target.revision_id),
    )
    assert newer.operation_id is not None
    published = await resolve(
        stopped, tmp_path, newer.operation_id, edit_request(SkillResolutionChoice(use="current"))
    )
    assert published.status == "published"
    old = await info(stopped, tmp_path, original.operation_id)
    assert old.status == "superseded" and old.replacement_id == newer.operation_id
    assert old.superseded_reason == "migration_baseline_changed" and old.original == original
    later = await migrate(
        stopped,
        tmp_path,
        await request(stopped, tmp_path, before.source.revision_id, before.target.revision_id),
    )
    assert later.status == "ready"
    assert (
        await info(stopped, tmp_path, original.operation_id)
    ).replacement_id == newer.operation_id
