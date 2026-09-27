"""
验证整侧人工选择不会跨越来源边界，组合依赖失败只保存待解决计划。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from skill_publication_support import baseline, directory_tree, ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_resolution_service import choose, pending, upload_tree
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_resolution import SkillResolutionPlan
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
)
from agent_remote_server.schemas.skill_library import SkillRemoveRequest
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.content import SkillContentError


@pytest.mark.parametrize("custom", [False, True])
async def test_whole_choice_cannot_revive_unchanged_pre_reset_branch(
    stopped: RuntimeHarness, tmp_path: Path, custom: bool
) -> None:
    """
    原提交未改动的分支被重置后，完整 incoming 或人工目录仍不能恢复旧内容。

    :param stopped (RuntimeHarness): 重置前会话
    :param tmp_path (Path): 私有内容卷
    :param custom (bool): 是否上传人工完整结果
    """
    other = await new_session(stopped, tmp_path)
    await publish(
        other,
        tmp_path,
        await ingest(other, tmp_path, {"learning/SKILL.md": b"reset", "aux": b"current"}),
    )
    async with stopped.database.begin() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None
        branch.epoch += 1
    publication = await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"aux": b"incoming"})
    )
    assert publication.status == "conflicted"
    before = await directory_tree(stopped, tmp_path)
    selection = SkillResolutionChoice(use="incoming")
    if custom:
        digest = await upload_tree(stopped, tmp_path, {"learning/SKILL.md": b"manual"})
        selection = SkillResolutionChoice(directory_tree_digest=digest)
    with pytest.raises(SkillContentError) as error:
        await choose(stopped, tmp_path, publication, selection)
    assert error.value.code == "STATE_EPOCH_CHANGED"
    assert await directory_tree(stopped, tmp_path) == before
    async with stopped.database() as session:
        assert await session.get(SkillResolutionPlan, publication.id) is None
    result = await choose(stopped, tmp_path, publication, SkillResolutionChoice(use="current"))
    assert result.status == "published"


async def test_custom_directory_cannot_delete_unexposed_member(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    另一会话新建的本地成员不属于原提交的写入范围，整目录人工选择也不能删除。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 私有内容卷
    """
    other = await new_session(stopped, tmp_path)
    publication = await pending(stopped, tmp_path)
    await publish(other, tmp_path, await ingest(other, tmp_path, {"notes/SKILL.md": b"new"}))
    stale = await choose(stopped, tmp_path, publication, SkillResolutionChoice(use="current"))
    assert stale.status == "superseded" and stale.replacement_id is not None
    async with stopped.database() as session:
        replacement = await session.get(SkillPublication, stale.replacement_id)
        assert replacement is not None and replacement.status == "conflicted"
    publication = replacement
    digest = await upload_tree(stopped, tmp_path, {"learning/SKILL.md": b"custom"})
    with pytest.raises(SkillContentError) as error:
        await choose(
            stopped, tmp_path, publication, SkillResolutionChoice(directory_tree_digest=digest)
        )
    assert error.value.code == "STATE_SCOPE_MISMATCH"
    assert "notes/SKILL.md" in {
        entry.path for entry in (await directory_tree(stopped, tmp_path)).entries
    }


async def test_removed_unchanged_source_is_not_projected_or_revived(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    来源移除后旧分支仍可保留恢复引用，但不得重新覆盖当前完整目录。

    :param stopped (RuntimeHarness): 包含旧来源的原始会话
    :param tmp_path (Path): 私有内容卷
    """
    await LibraryHarness(stopped.database, tmp_path, stopped.owner).execute(
        SkillRemoveRequest(skill="learning", expected_generation=1, idempotency_key=str(uuid4()))
    )
    digest = await upload_tree(stopped, tmp_path, {"aux": b"current"})
    async with stopped.database.begin() as session:
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        assert directory is not None
        replacement = SkillCheckpoint(
            id=uuid4(),
            user_id=stopped.owner,
            account_id=stopped.account,
            scope="directory",
            tree_digest=digest,
            content_digest=digest,
            parent_id=directory.head_checkpoint_id,
        )
        session.add(replacement)
        await session.flush()
        directory.head_checkpoint_id = replacement.id
    publication = await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"aux": b"incoming"})
    )
    assert publication.status == "conflicted" and publication.current_tree_digest == digest
    with pytest.raises(SkillContentError) as error:
        await choose(stopped, tmp_path, publication, SkillResolutionChoice(use="incoming"))
    assert error.value.code == "STATE_SCOPE_MISMATCH"
    result = await choose(
        stopped, tmp_path, publication, SkillResolutionChoice(path="aux", use="incoming")
    )
    assert result.status == "published"
    assert {entry.path for entry in (await directory_tree(stopped, tmp_path)).entries} == {"aux"}


async def test_combined_link_cycle_saves_pending_plan_then_accepts_whole_repair(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    各侧合法而选择组合形成循环时保留完整计划和旧 head，整体修复后才发布。

    :param stopped (RuntimeHarness): 初始会话
    :param tmp_path (Path): 私有内容卷
    """
    await publish(
        stopped,
        tmp_path,
        await ingest(stopped, tmp_path, {"learning/one": b"old", "learning/two": b"old"}),
    )
    left = await new_session(stopped, tmp_path)
    right = await new_session(stopped, tmp_path)
    await publish(
        left,
        tmp_path,
        await ingest(left, tmp_path, {"learning/two": b"left"}, links={"learning/one": "two"}),
    )
    publication = await publish(
        right,
        tmp_path,
        await ingest(right, tmp_path, {"learning/one": b"right"}, links={"learning/two": "one"}),
    )
    before = await directory_tree(stopped, tmp_path)
    first = await choose(
        stopped, tmp_path, publication, SkillResolutionChoice(path="learning/one", use="current")
    )
    assert first.status == "pending"
    second = await choose(
        stopped,
        tmp_path,
        publication,
        SkillResolutionChoice(path="learning/two", use="incoming"),
        revision=1,
    )
    assert second.status == "pending" and second.remaining[0].reason == "invalid_tree"
    assert second.result_tree_digest is None
    assert await directory_tree(stopped, tmp_path) == before
    fixed = await choose(
        stopped, tmp_path, publication, SkillResolutionChoice(use="current"), revision=2
    )
    assert fixed.status == "published" and len(fixed.choices) == 1
    assert await directory_tree(stopped, tmp_path) == before
    next_session = await new_session(stopped, tmp_path)
    assert (await baseline(next_session, tmp_path)).entries == before.entries
