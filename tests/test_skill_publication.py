"""
验证完整发布、跨会话合并、epoch 归档以及账户本地来源原子激活。
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import baseline, directory_tree, ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_local import source
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_publications import SkillPublicationBranch
from agent_remote_server.models.skill_snapshots import SkillFinalization
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_library import SkillRemoveRequest, SkillUpdateRequest
from agent_remote_server.services.skills.content import SkillContentError


async def test_publication_advances_exact_heads_and_retries_without_extra_checkpoints(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    真实上传完成后推进目录和原始分支，重复并发发布复用一份结果。

    :param stopped (RuntimeHarness): 已停止精确会话
    :param tmp_path (Path): 内容卷
    """
    receipt = await ingest(stopped, tmp_path, {"learning/new.txt": b"learned"})
    first, second = await asyncio.gather(
        publish(stopped, tmp_path, receipt), publish(stopped, tmp_path, receipt)
    )
    assert first.id == second.id and first.status == "published"
    async with stopped.database() as session:
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        branch = await session.get(AccountSkillState, stopped.state)
        assert directory is not None and directory.head_checkpoint_id == first.result_checkpoint_id
        assert branch is not None and branch.head_checkpoint_id is not None
        checkpoint = await session.get(SkillCheckpoint, branch.head_checkpoint_id)
        assert checkpoint is not None and checkpoint.source_session_reference_id == stopped.session
        precondition = await session.get(SkillPublicationBranch, (first.id, stopped.state))
        assert precondition is not None and precondition.changed
        assert precondition.expected_checkpoint_id != branch.head_checkpoint_id
    next_session = await new_session(stopped, tmp_path)
    assert any(
        entry.path == "learning/new.txt"
        for entry in (await baseline(next_session, tmp_path)).entries
    )


async def test_concurrent_different_paths_merge_without_overwriting_either_input(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    同基线的不同文件修改保留双方完整内容，不按最后到达覆盖整树。

    :param stopped (RuntimeHarness): 第一个终态会话
    :param tmp_path (Path): 内容卷
    """
    other = await new_session(stopped, tmp_path)
    first = await ingest(stopped, tmp_path, {"learning/first.txt": b"one"})
    second = await ingest(other, tmp_path, {"learning/second.txt": b"two"})
    assert (await publish(stopped, tmp_path, first)).status == "published"
    assert (await publish(other, tmp_path, second)).status == "published"
    paths = {entry.path for entry in (await directory_tree(stopped, tmp_path)).entries}
    assert {"learning/first.txt", "learning/second.txt"} <= paths


async def test_conflict_retains_entire_input_and_does_not_activate_unrelated_new_skill(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    一个路径冲突就冻结整个目录提交，新技能候选也不能提前激活。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    other = await new_session(stopped, tmp_path)
    first = await ingest(stopped, tmp_path, {"learning/SKILL.md": b"first"})
    second = await ingest(
        other, tmp_path, {"learning/SKILL.md": b"second", "notes/SKILL.md": b"new"}
    )
    published = await publish(stopped, tmp_path, first)
    conflict = await publish(other, tmp_path, second)
    assert conflict.status == "conflicted" and conflict.result_checkpoint_id is None
    assert any(item["path"] == "learning/SKILL.md" for item in conflict.conflicts_json)
    async with stopped.database() as session:
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        assert (
            directory is not None and directory.head_checkpoint_id == published.result_checkpoint_id
        )
        candidates = (
            await session.scalars(
                select(AccountLocalSkill).where(AccountLocalSkill.account_id == stopped.account)
            )
        ).all()
        assert len(candidates) == 1 and candidates[0].status == "staged"
        receipt = await session.get(SkillFinalization, second)
        assert (
            receipt is not None
            and receipt.tree_digest is not None
            and receipt.status == "conflicted"
        )
    assert (await publish(other, tmp_path, second)).id == conflict.id


@pytest.mark.parametrize("cause", ["unclean", "directory_epoch", "state_epoch", "source_removed"])
async def test_invalid_write_detaches_entire_submission(
    stopped: RuntimeHarness,
    tmp_path: Path,
    cause: str,
) -> None:
    """
    任一写入失效时整份输入归档，不能发布另一项或新建本地身份。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    :param cause (str): 整体归档触发条件
    """
    receipt = await ingest(
        stopped,
        tmp_path,
        {"learning/new.txt": b"learned", "notes/SKILL.md": b"new"},
        unclean=cause == "unclean",
    )
    async with stopped.database.begin() as session:
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        branch = await session.get(AccountSkillState, stopped.state)
        assert directory is not None and branch is not None
        old_head = directory.head_checkpoint_id
        old_branch = branch.head_checkpoint_id
        if cause == "directory_epoch":
            directory.epoch += 1
        elif cause == "state_epoch":
            branch.epoch += 1
    if cause == "source_removed":
        await LibraryHarness(stopped.database, tmp_path, stopped.owner).execute(
            SkillRemoveRequest(
                skill="learning",
                expected_generation=1,
                idempotency_key=str(uuid4()),
            )
        )
    result = await publish(stopped, tmp_path, receipt)
    assert result.status == "detached" and result.reason is not None
    async with stopped.database() as session:
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        branch = await session.get(AccountSkillState, stopped.state)
        assert directory is not None and directory.head_checkpoint_id == old_head
        assert branch is not None and branch.head_checkpoint_id == old_branch
        assert not (
            await session.scalars(
                select(AccountLocalSkill).where(AccountLocalSkill.account_id == stopped.account)
            )
        ).all()


async def test_unchanged_reset_branch_does_not_block_auxiliary_write(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    原快照未改动的条目不算本次写入，不能因该项 epoch 变化拒绝根数据。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    receipt = await ingest(stopped, tmp_path, {"aux.txt": b"root state"})
    reset = await source(stopped, tmp_path, "learning", content=b"reset instructions")
    async with stopped.database.begin() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None
        replacement = SkillCheckpoint(
            id=uuid4(),
            user_id=stopped.owner,
            account_id=stopped.account,
            scope="item",
            state_id=branch.id,
            subtree_prefix="learning",
            content_digest=reset.content_digest,
            tree_digest=reset.tree_digest,
        )
        session.add(replacement)
        await session.flush()
        branch.epoch += 1
        branch.head_checkpoint_id = replacement.id
        old_head = replacement.id
    result = await publish(stopped, tmp_path, receipt)
    assert result.status == "published"
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None and branch.head_checkpoint_id == old_head
        condition = await session.get(SkillPublicationBranch, (result.id, stopped.state))
        assert condition is not None and not condition.changed


async def test_late_old_revision_write_stays_on_original_branch(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    默认上游切换不使旧会话失效，也不把旧会话改动写入新 revision。

    :param stopped (RuntimeHarness): 旧版本会话
    :param tmp_path (Path): 内容卷
    """
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    old = await library.info()
    await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=await library.candidate(version="two"),
            expected_generation=1,
            idempotency_key=str(uuid4()),
        )
    )
    latest = await library.info()
    migrated = await source(stopped, tmp_path, "learning", content=b"new revision migrated state")
    async with stopped.database.begin() as session:
        branch = AccountSkillState(
            id=uuid4(),
            user_id=stopped.owner,
            account_id=stopped.account,
            installation_id=latest.id,
            installation_epoch=latest.epoch,
            base_revision_id=latest.default_revision_id,
        )
        session.add(branch)
        await session.flush()
        view = SkillCheckpoint(
            id=uuid4(),
            user_id=stopped.owner,
            account_id=stopped.account,
            scope="item",
            state_id=branch.id,
            subtree_prefix="learning",
            content_digest=migrated.content_digest,
            tree_digest=migrated.tree_digest,
        )
        session.add(view)
        await session.flush()
        branch.head_checkpoint_id = view.id
        migrated_branch_id, migrated_head_id = branch.id, view.id
        session.add(
            SkillDirectoryMember(
                user_id=stopped.owner,
                account_id=stopped.account,
                directory_checkpoint_id=migrated.id,
                state_id=branch.id,
                checkpoint_id=view.id,
                entry_name="learning",
            )
        )
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        assert directory is not None
        directory.head_checkpoint_id = migrated.id
    receipt = await ingest(stopped, tmp_path, {"learning/old.txt": b"old revision state"})
    assert (await publish(stopped, tmp_path, receipt)).status == "published"
    async with stopped.database() as session:
        migrated_branch = await session.get(AccountSkillState, migrated_branch_id)
        assert (
            migrated_branch is not None and migrated_branch.head_checkpoint_id == migrated_head_id
        )
    next_session = await new_session(stopped, tmp_path)
    assert not any(
        entry.path == "learning/old.txt"
        for entry in (await baseline(next_session, tmp_path)).entries
    )
    async with stopped.database() as session:
        original_branch = await session.get(AccountSkillState, stopped.state)
        assert (
            original_branch is not None
            and original_branch.base_revision_id == old.default_revision_id
        )
        assert (await library.info()).default_revision_id != original_branch.base_revision_id


async def test_new_skill_activation_and_unexposed_member_survive_another_publication(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    新本地条目随目录原子激活，未暴露它的旧会话不会把它当作删除。

    :param stopped (RuntimeHarness): 第一份会话
    :param tmp_path (Path): 内容卷
    """
    other = await new_session(stopped, tmp_path)
    first = await ingest(stopped, tmp_path, {"notes/SKILL.md": b"new instructions"})
    second = await ingest(other, tmp_path, {"learning/new.txt": b"learned"})
    assert (await publish(stopped, tmp_path, first)).status == "published"
    assert (await publish(other, tmp_path, second)).status == "published"
    next_session = await new_session(stopped, tmp_path)
    paths = {entry.path for entry in (await baseline(next_session, tmp_path)).entries}
    assert {"notes/SKILL.md", "learning/new.txt"} <= paths
    async with stopped.database() as session:
        local = await session.scalar(
            select(AccountLocalSkill).where(AccountLocalSkill.account_id == stopped.account)
        )
        assert local is not None and local.status == "active"


async def test_same_new_name_from_concurrent_sessions_is_a_source_conflict(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    新技能内容甚至完全相同也不能让不同创建来源共享一个身份。

    :param stopped (RuntimeHarness): 第一份会话
    :param tmp_path (Path): 内容卷
    """
    other = await new_session(stopped, tmp_path)
    first = await ingest(stopped, tmp_path, {"notes/SKILL.md": b"same"})
    second = await ingest(other, tmp_path, {"notes/SKILL.md": b"same"})
    await publish(stopped, tmp_path, first)
    result = await publish(other, tmp_path, second)
    assert result.status == "conflicted"
    assert any(item["reason"] == "source_conflict" for item in result.conflicts_json)
    async with stopped.database() as session:
        candidates = (
            await session.scalars(
                select(AccountLocalSkill).where(AccountLocalSkill.account_id == stopped.account)
            )
        ).all()
        assert sorted(item.status for item in candidates) == ["active", "staged"]


async def test_unchanged_input_does_not_advance_any_head(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    单纯会话复制不生成虚假的已发布状态更新。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    receipt = await ingest(stopped, tmp_path, {})
    result = await publish(stopped, tmp_path, receipt)
    assert result.status == "published" and result.result_checkpoint_id == stopped.directory


async def test_unknown_finalization_cannot_publish(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    猜测收尾身份不授予发布权限。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    with pytest.raises(SkillContentError) as error:
        await publish(stopped, tmp_path, uuid4())
    assert error.value.code == "FINALIZATION_NOT_FOUND"


async def test_directory_cas_failure_rolls_back_all_branch_writes(
    stopped: RuntimeHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    最后目录 CAS 失败时，前面的分支更新和新身份激活也必须全部回滚。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 受控模拟并发条件失败
    """

    async def fail_directory(
        self: SkillPublicationRepository,
        directory: AccountSkillDirectoryState,
        checkpoint_id: UUID,
    ) -> bool:
        """
        模拟最终目录比较交换未命中。

        :param directory (AccountSkillDirectoryState): 旧目标
        :param checkpoint_id (UUID): 待发布结果
        :return bool: 未命中比较交换
        """
        return False

    receipt_id = await ingest(
        stopped, tmp_path, {"learning/new.txt": b"learned", "notes/SKILL.md": b"new"}
    )
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None
        original = branch.head_checkpoint_id
    monkeypatch.setattr(SkillPublicationRepository, "advance_directory", fail_directory)
    with pytest.raises(SkillContentError) as error:
        await publish(stopped, tmp_path, receipt_id)
    assert error.value.code == "HEAD_CHANGED"
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        receipt = await session.get(SkillFinalization, receipt_id)
        assert branch is not None and branch.head_checkpoint_id == original
        assert receipt is not None and receipt.status == "persisted"
        assert not (
            await session.scalars(
                select(AccountLocalSkill).where(AccountLocalSkill.account_id == stopped.account)
            )
        ).all()


async def test_binary_divergence_preserves_current_head_and_complete_incoming(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    同一技能数据库与其他文件的并发修改不能自动拼接为完整发布。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    seed = await ingest(stopped, tmp_path, {"learning/state.db": b"\x00old"})
    await publish(stopped, tmp_path, seed)
    first = await new_session(stopped, tmp_path)
    second = await new_session(stopped, tmp_path)
    current = await publish(
        first, tmp_path, await ingest(first, tmp_path, {"learning/state.db": b"\x00new"})
    )
    conflict = await publish(
        second, tmp_path, await ingest(second, tmp_path, {"learning/readme.txt": b"text edit"})
    )
    assert conflict.status == "conflicted"
    assert any(item["reason"] == "opaque_divergence" for item in conflict.conflicts_json)
    async with stopped.database() as session:
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        assert (
            directory is not None and directory.head_checkpoint_id == current.result_checkpoint_id
        )


@pytest.mark.parametrize("delete", [True, False])
async def test_deleted_or_invalid_known_skill_keeps_its_branch_identity(
    stopped: RuntimeHarness,
    tmp_path: Path,
    delete: bool,
) -> None:
    """
    已知技能删除或损坏说明后仍保存同分支状态，下一次启动不能静默恢复原始包。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    :param delete (bool): 是否删除整个目录
    """
    changes: dict[str, bytes | None] = (
        {"learning": None} if delete else {"learning/SKILL.md": b"---\nbroken header"}
    )
    result = await publish(stopped, tmp_path, await ingest(stopped, tmp_path, changes))
    assert result.status == "published"
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None and branch.head_checkpoint_id is not None
        checkpoint = await session.get(SkillCheckpoint, branch.head_checkpoint_id)
        assert checkpoint is not None and checkpoint.invalid_skill_format
    with pytest.raises(SkillContentError):
        await new_session(stopped, tmp_path)
