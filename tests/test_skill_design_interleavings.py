"""
将设计 11.1 中的纪元与目录组合场景落实到实际命令、快照和收尾事务。
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import baseline, directory_tree, ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_preparation import prepare, request, update_version
from test_skill_snapshots import prepared as prepared
from test_skill_snapshots import reserve
from test_skill_state_commands import command, execute
from test_skill_storage import file_entry

from agent_remote_server.models.skill_snapshots import SessionSkillSnapshotItem, SkillFinalization
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.schemas.skill_library import (
    SkillRemoveRequest,
    SkillRuleRequest,
    SkillScope,
)
from agent_remote_server.services.skills.content import SkillContentError


async def assert_retained_files(
    state: RuntimeHarness, root: Path, receipt: UUID, files: dict[str, bytes]
) -> None:
    """
    通过原收尾引用读取完整树和真实文件，防止只保留名称而丢失未发布数据。

    :param state (RuntimeHarness): 原用户与会话
    :param root (Path): 私有内容卷
    :param receipt (UUID): 原始收尾身份
    :param files (dict[str, bytes]): 必须完整保留的内容
    """
    import io

    async with state.database() as session:
        record = await session.get(SkillFinalization, receipt)
        assert record is not None and record.tree_digest is not None
        service = content_service(session, root)
        manifest = await service.read_tree(state.owner, "state", record.tree_digest)
        entries = {entry.path: entry for entry in manifest.entries}
        for path, value in files.items():
            assert entries[path] == file_entry(value, path=path)
            output = io.BytesIO()
            await service.read_file(
                state.owner, "state", record.tree_digest, entries[path].sha256, output
            )
            assert output.getvalue() == value


async def test_reinstall_then_old_session_completion_never_advances_new_epoch(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    删除 E1 后重装并初始化 E2，旧会话晚到内容只归档而不能推进 E2。

    :param stopped (RuntimeHarness): E1 原始会话
    :param tmp_path (Path): 独立内容卷
    """
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRemoveRequest(
            skill="learning",
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    await library.add(await library.candidate())
    reset = await command(stopped, tmp_path)
    assert reset.expected.targets[0].installation_epoch == 2
    await execute(stopped, tmp_path, reset)
    current = await command(stopped, tmp_path)
    head = current.expected.targets[0].head_checkpoint_id
    before = await directory_tree(stopped, tmp_path)
    files = {"learning/late.txt": b"retained old epoch"}
    receipt = await ingest(stopped, tmp_path, dict(files))
    result = await publish(stopped, tmp_path, receipt)
    assert result.status == "detached"
    after = await command(stopped, tmp_path)
    assert after.expected.targets[0].installation_epoch == 2
    assert after.expected.targets[0].head_checkpoint_id == head
    assert await directory_tree(stopped, tmp_path) == before
    await assert_retained_files(stopped, tmp_path, receipt, files)
    async with stopped.database() as session:
        old = await session.get(AccountSkillState, stopped.state)
        assert old is not None and old.installation_epoch == 1


async def test_reset_one_of_two_existing_skills_detaches_both_late_changes(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    同一原目录暴露两个已存在条目，重置一个后另一个晚到改动也不能单独发布。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    """
    await publish(
        stopped,
        tmp_path,
        await ingest(stopped, tmp_path, {"notes/SKILL.md": b"---\nname: notes\n---\nNotes\n"}),
    )
    late = await new_session(stopped, tmp_path)
    assert {"learning/SKILL.md", "notes/SKILL.md"} <= {
        entry.path for entry in (await baseline(late, tmp_path)).entries
    }
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    before = await directory_tree(stopped, tmp_path)
    async with stopped.database() as session:
        heads = dict(
            (
                await session.execute(
                    select(AccountSkillState.id, AccountSkillState.head_checkpoint_id).where(
                        AccountSkillState.account_id == stopped.account
                    )
                )
            )
            .tuples()
            .all()
        )
    files = {"learning/memory": b"late A", "notes/memory": b"late B"}
    receipt = await ingest(late, tmp_path, dict(files))
    result = await publish(late, tmp_path, receipt)
    assert result.status == "detached" and result.reason == "state_epoch_changed"
    assert await directory_tree(stopped, tmp_path) == before
    async with stopped.database() as session:
        for identity, head in heads.items():
            branch = await session.get(AccountSkillState, identity)
            assert branch is not None and branch.head_checkpoint_id == head
    await assert_retained_files(late, tmp_path, receipt, files)


async def test_directory_reset_prevents_late_root_auxiliary_resurrection(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    真实目录重置推进纪元，旧会话仅修改根辅助文件也只能完整归档。

    :param stopped (RuntimeHarness): 原账户和精确会话
    :param tmp_path (Path): 内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"root-state": b"old root"}))
    late = await new_session(stopped, tmp_path)
    reset = await command(stopped, tmp_path, directory=True)
    await execute(stopped, tmp_path, reset)
    current = await command(stopped, tmp_path, directory=True)
    assert reset.expected.directory_epoch is not None
    assert current.expected.directory_epoch == reset.expected.directory_epoch + 1
    before = await directory_tree(stopped, tmp_path)
    assert "root-state" not in {entry.path for entry in before.entries}
    files = {"root-state": b"late root change"}
    receipt = await ingest(late, tmp_path, dict(files))
    result = await publish(late, tmp_path, receipt)
    assert result.status == "detached" and result.reason == "directory_epoch_changed"
    assert await directory_tree(stopped, tmp_path) == before
    await assert_retained_files(late, tmp_path, receipt, files)


async def test_disabled_unexposed_skill_is_not_a_session_deletion(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    账户停用后新快照不暴露原技能，收尾新增根文件不能删除已有学习分支。

    :param stopped (RuntimeHarness): 已存在学习数据的账户
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"keep learning"})
    )
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None
        head = branch.head_checkpoint_id
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="learning",
            scope=SkillScope(account_id=stopped.account),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    empty = await new_session(stopped, tmp_path)
    assert not any(
        entry.path.startswith("learning/") for entry in (await baseline(empty, tmp_path)).entries
    )
    result = await publish(
        empty, tmp_path, await ingest(empty, tmp_path, {"root-state": b"new root"})
    )
    assert result.status == "published"
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None and branch.head_checkpoint_id == head
    tree = await directory_tree(stopped, tmp_path)
    assert {"learning/memory", "root-state"} <= {entry.path for entry in tree.entries}


@pytest.mark.parametrize("change_before", [True, False])
async def test_default_revision_change_obeys_snapshot_transaction_boundary(
    stopped: RuntimeHarness, tmp_path: Path, change_before: bool
) -> None:
    """
    r2 准备后切换 r3，快照前的新配置生效，快照后的切换不改写原会话。

    :param stopped (RuntimeHarness): 已有原分支的账户
    :param tmp_path (Path): 内容卷
    :param change_before (bool): 是否在精确快照之前切换默认版本
    """
    r2 = await update_version(stopped, tmp_path, "two")
    assert (await prepare(stopped, tmp_path, await request(stopped, tmp_path))).status == "ready"
    original = None if change_before else await new_session(stopped, tmp_path)
    r3 = await update_version(stopped, tmp_path, "three")
    assert (await prepare(stopped, tmp_path, await request(stopped, tmp_path))).status == "ready"
    selected = await new_session(stopped, tmp_path) if original is None else original
    replay = await reserve(selected, tmp_path)
    assert replay.id == selected.snapshot
    async with stopped.database() as session:
        item = await session.scalar(
            select(SessionSkillSnapshotItem).where(
                SessionSkillSnapshotItem.snapshot_id == selected.snapshot
            )
        )
        assert item is not None
        branch = await session.get(AccountSkillState, item.state_id)
        assert branch is not None and branch.base_revision_id == (r3 if change_before else r2)
    info = await LibraryHarness(stopped.database, tmp_path, stopped.owner).info()
    assert info.default_revision_id == r3


async def test_disabling_link_target_preserves_history_and_refuses_new_snapshot(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    完整目录中的 A 链接到 B，停用 B 后不能偷带 B 或破坏历史以允许新会话。

    :param stopped (RuntimeHarness): 原账户与目录
    :param tmp_path (Path): 内容卷
    """
    result = await publish(
        stopped,
        tmp_path,
        await ingest(
            stopped,
            tmp_path,
            {"notes/SKILL.md": b"Notes"},
            links={"notes/reference": "../learning/SKILL.md"},
        ),
    )
    assert result.status == "published"
    before = await directory_tree(stopped, tmp_path)
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="learning",
            scope=SkillScope(account_id=stopped.account),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    with pytest.raises(SkillContentError) as error:
        await new_session(stopped, tmp_path)
    assert error.value.code == "STATE_DEPENDENCY_MISSING"
    assert await directory_tree(stopped, tmp_path) == before
    info = await library.info(account=stopped.account)
    assert info.effective is not None and not info.effective.enabled


async def test_conflict_in_one_existing_skill_withholds_both_changes_from_next_session(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    两个已存在条目共同写入，任一冲突使整份输入保留，新会话仍继承原完整发布。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"notes/SKILL.md": b"Notes"}))
    first, second = await new_session(stopped, tmp_path), await new_session(stopped, tmp_path)
    await publish(first, tmp_path, await ingest(first, tmp_path, {"learning/shared": b"first"}))
    before = await directory_tree(stopped, tmp_path)
    files = {"learning/shared": b"second", "notes/memory": b"withheld second skill"}
    receipt = await ingest(second, tmp_path, dict(files))
    async with stopped.database() as session:
        saved = await session.get(SkillFinalization, receipt)
        assert saved is not None and saved.status == "persisted"
    result = await publish(second, tmp_path, receipt)
    assert result.status == "conflicted" and result.result_checkpoint_id is None
    assert await directory_tree(stopped, tmp_path) == before
    next_session = await new_session(stopped, tmp_path)
    assert await baseline(next_session, tmp_path) == before
    await assert_retained_files(second, tmp_path, receipt, files)


async def test_directory_restore_after_revision_switch_rejects_without_changing_rules(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    整目录历史属于旧原始版本时拒绝恢复，不能隐式回滚规则或部分覆盖新版分支。

    :param stopped (RuntimeHarness): 原版本账户
    :param tmp_path (Path): 私有内容卷
    """
    original = await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"old learning"})
    )
    assert original.result_checkpoint_id is not None
    await update_version(stopped, tmp_path, "two")
    assert (await prepare(stopped, tmp_path, await request(stopped, tmp_path))).status == "ready"
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    configuration = await library.info(account=stopped.account)
    generation = await library.generation()
    before = await directory_tree(stopped, tmp_path)
    restore = await command(
        stopped, tmp_path, directory=True, checkpoint=original.result_checkpoint_id
    )
    with pytest.raises(SkillContentError) as error:
        await execute(stopped, tmp_path, restore)
    assert error.value.code == "STATE_SCOPE_MISMATCH"
    assert await directory_tree(stopped, tmp_path) == before
    assert await library.info(account=stopped.account) == configuration
    assert await library.generation() == generation
