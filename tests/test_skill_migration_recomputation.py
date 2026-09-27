"""
验证过期迁移从保留输入重新比较，旧选择、纪元和成功基线不能越过替代边界。
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_finalization import stopped as stopped
from test_skill_migration_conflicts import counts, pending
from test_skill_migration_resolution_drafts import edit, edit_request, saved_plan, two_conflicts
from test_skill_migration_resolution_preparation import initial_conflict
from test_skill_migration_resolution_service import resolve
from test_skill_preparation import pin, prepare, request, update_version
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute

from agent_remote_server.models.skill_migration_resolution import SkillMigrationResolutionOperation
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.repositories.skill_migration_resolution import (
    SkillMigrationResolutionRepository,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_migration_conflicts import SkillMigrationConflictView
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_conflicts import SkillMigrationConflictService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def info(state: RuntimeHarness, root: Path, identity: UUID) -> SkillMigrationConflictView:
    """
    独立读取原输入、替代身份和实时诊断，避免依赖 ORM 缓存。

    :param state (RuntimeHarness): 原账户
    :param root (Path): 内容卷
    :param identity (UUID): 已保留尝试
    :return SkillMigrationConflictView: 完整原始与实时视图
    """
    async with state.database.begin() as session:
        return await SkillMigrationConflictService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).info(state.owner, identity)


async def test_target_head_recomputation_discards_choices_and_preserves_all_old_inputs(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    旧计划与本次请求均不应用到新目标，唯一替代保留原输入并允许重新明确解决。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    identity = await two_conflicts(stopped, tmp_path)
    original = await info(stopped, tmp_path, identity)
    await edit(
        stopped,
        tmp_path,
        identity,
        edit_request(SkillResolutionChoice(path="learning/a", use="incoming")),
    )
    old_plan = await saved_plan(stopped, identity)
    newer = await new_session(stopped, tmp_path)
    published = await publish(
        newer, tmp_path, await ingest(newer, tmp_path, {"learning/a": b"new current"})
    )
    assert published.status == "published"
    heads = (await command(stopped, tmp_path)).expected
    payload = edit_request(SkillResolutionChoice(path="learning/b", use="current"), revision=1)
    before = await counts(stopped)
    preview = await resolve(
        stopped, tmp_path, identity, payload.model_copy(update={"dry_run": True})
    )
    assert (
        preview.status == "preview"
        and preview.recomputation_possible
        and preview.replacement_id is None
    )
    assert await counts(stopped) == before and await saved_plan(stopped, identity) == old_plan
    first, concurrent = await asyncio.gather(
        resolve(stopped, tmp_path, identity, payload), resolve(stopped, tmp_path, identity, payload)
    )
    assert first == concurrent and first.status == "superseded" and first.replacement_id is not None
    replacement = first.replacement_id
    assert await saved_plan(stopped, replacement) == (0, [], 0)
    assert (await command(stopped, tmp_path)).expected == heads
    old = await info(stopped, tmp_path, identity)
    assert (
        old.original == original.original
        and old.base == original.base
        and old.incoming == original.incoming
    )
    assert old.current == original.current and old.replacement_id == replacement
    fresh = await info(stopped, tmp_path, replacement)
    assert fresh.recomputed_from_id == identity and fresh.status == "conflicted"
    assert (
        fresh.current.source == "target_published"
        and fresh.current.checkpoint_id == heads.targets[0].head_checkpoint_id
    )
    assert fresh.incoming.checkpoint_id == original.incoming.checkpoint_id
    assert not fresh.live.recomputation_reasons
    retry = await resolve(stopped, tmp_path, identity, edit_request(revision=1))
    assert retry.replacement_id == replacement and not retry.recomputation_possible
    resolved = await resolve(
        stopped, tmp_path, replacement, edit_request(SkillResolutionChoice(use="current"))
    )
    assert resolved.status == "published" and resolved.migration_sequence == 2
    assert await resolve(stopped, tmp_path, identity, payload) == first


@pytest.mark.parametrize("mode", ["forward", "incremental"])
async def test_new_source_head_never_replaces_saved_incoming_during_recomputation(
    stopped: RuntimeHarness, tmp_path: Path, mode: str
) -> None:
    """
    来源晚到发布使目录变化，但替代比较仍只读取旧尝试确切 source checkpoint。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param mode (str): 初次向前或显式增量迁移
    """
    late = await new_session(stopped, tmp_path)
    original = await pending(stopped, tmp_path, mode)
    identity = original.operation_id
    assert identity is not None
    before = await info(stopped, tmp_path, identity)
    await publish(
        late, tmp_path, await ingest(late, tmp_path, {"learning/late": b"later source data"})
    )
    result = await resolve(stopped, tmp_path, identity, edit_request())
    assert result.replacement_id is not None
    fresh = await info(stopped, tmp_path, result.replacement_id)
    assert fresh.incoming.checkpoint_id == before.incoming.checkpoint_id
    assert fresh.incoming.tree_digest == before.incoming.tree_digest
    assert fresh.live.source_head_advanced and not fresh.live.recomputation_reasons
    async with stopped.database() as session:
        old = await session.get(SkillBranchPreparation, identity)
        new = await session.get(SkillBranchPreparation, result.replacement_id)
        assert old is not None and new is not None
        assert new.source_checkpoint_id == old.source_checkpoint_id
        assert new.source_epoch == old.source_epoch and new.mode == old.mode
        assert old.response_json == original.model_dump(mode="json")


async def test_clean_recomparison_publishes_new_attempt_without_using_submitted_choice(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    当前侧自然消除冲突时发布原输入重算结果，不执行旧请求中的任意覆盖选择。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    identity = await two_conflicts(stopped, tmp_path)
    original = await info(stopped, tmp_path, identity)
    newer = await new_session(stopped, tmp_path)
    await publish(
        newer,
        tmp_path,
        await ingest(
            newer,
            tmp_path,
            {
                "learning/a": b"theirs",
                "learning/b": b"theirs",
                "learning/keep": b"new current only",
            },
        ),
    )
    result = await resolve(
        stopped,
        tmp_path,
        identity,
        edit_request(SkillResolutionChoice(directory_tree_digest="0" * 64)),
    )
    assert result.status == "superseded" and result.replacement_id is not None
    replacement = await info(stopped, tmp_path, result.replacement_id)
    assert replacement.status == "ready" and replacement.original.status == "ready"
    assert isinstance(replacement.original, SkillMigrationView)
    assert (
        replacement.original.migration_sequence == 2 and not replacement.live.recomputation_reasons
    )
    assert replacement.incoming.checkpoint_id == original.incoming.checkpoint_id
    assert await saved_plan(stopped, result.replacement_id) == (0, [], 0)
    assert "learning/keep" not in {change.path for change in replacement.original.changes or []}


@pytest.mark.parametrize("directory", [False, True])
async def test_reset_terminates_attempt_without_recomputation_or_choice_application(
    stopped: RuntimeHarness, tmp_path: Path, directory: bool
) -> None:
    """
    单项或整个目录 reset 后只保留旧输入，没有新的有效比较或发布路径。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param directory (bool): 是否整个目录重置
    """
    original = await pending(stopped, tmp_path)
    identity = original.operation_id
    assert identity is not None
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path, directory=directory))
    heads = (await command(stopped, tmp_path, directory=True)).expected
    rows = await counts(stopped)
    result = await resolve(stopped, tmp_path, identity, edit_request())
    assert result.status == "superseded" and result.replacement_id is None
    assert result.stale_reasons == ("state_reset",) and not result.recomputation_possible
    assert await counts(stopped) == rows
    assert (await command(stopped, tmp_path, directory=True)).expected == heads
    assert (await info(stopped, tmp_path, identity)).original == original


async def test_recomputed_publication_and_replacement_links_rollback_on_late_receipt_failure(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    自动重算已发布之后回执失败，旧失效、新尝试、全部 head 和成功基线仍一起回滚。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 最终故障注入
    """
    identity = await two_conflicts(stopped, tmp_path)
    newer = await new_session(stopped, tmp_path)
    await publish(
        newer,
        tmp_path,
        await ingest(newer, tmp_path, {"learning/a": b"theirs", "learning/b": b"theirs"}),
    )
    before = await counts(stopped)
    heads = (await command(stopped, tmp_path)).expected
    save = SkillMigrationResolutionRepository.save_operation

    async def fail(
        self: SkillMigrationResolutionRepository, operation: SkillMigrationResolutionOperation
    ) -> None:
        """
        在替代发布及回执插入后模拟故障。

        :param operation (SkillMigrationResolutionOperation): 本次失效回执
        """
        await save(self, operation)
        raise SkillContentError("HEAD_CHANGED", "injected recomputation failure")

    with monkeypatch.context() as patch:
        patch.setattr(SkillMigrationResolutionRepository, "save_operation", fail)
        with pytest.raises(SkillContentError, match="injected recomputation failure"):
            await resolve(stopped, tmp_path, identity, edit_request())
    assert await counts(stopped) == before
    assert (await command(stopped, tmp_path)).expected == heads
    old = await info(stopped, tmp_path, identity)
    assert old.status == "conflicted" and old.replacement_id is None
    assert await saved_plan(stopped, identity) == (0, [], 0)


async def remove_auxiliary_link(state: RuntimeHarness, root: Path) -> None:
    """
    模拟同纪元完整目录辅助链接删除，单项内容与所有稳定成员引用保持不变。

    :param state (RuntimeHarness): 当前账户
    :param root (Path): 内容卷
    """
    async with state.database.begin() as session:
        service = content_service(session, root)
        directory = await session.get(AccountSkillDirectoryState, state.account)
        assert directory is not None and directory.head_checkpoint_id is not None
        old = await session.get(SkillCheckpoint, directory.head_checkpoint_id)
        assert old is not None and old.tree_digest is not None
        tree = await service.read_tree(state.owner, "state", old.tree_digest)
        result = SkillTreeManifest(
            entries=tuple(entry for entry in tree.entries if entry.path != "aux")
        )
        upload = await service.begin(state.owner, str(uuid4()), result, "state")
        stored = await service.complete(state.owner, upload.id)
        replacement = SkillCheckpoint(
            user_id=state.owner,
            account_id=state.account,
            scope="directory",
            content_digest=stored.digest,
            tree_digest=stored.digest,
            directory_epoch=directory.epoch,
            parent_id=old.id,
        )
        session.add(replacement)
        await session.flush()
        queries = SkillMigrationConflictService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).queries
        for member in await queries.runtime.members(old):
            session.add(
                SkillDirectoryMember(
                    user_id=state.owner,
                    account_id=state.account,
                    directory_checkpoint_id=replacement.id,
                    entry_name=member.entry_name,
                    state_id=member.state_id,
                    checkpoint_id=member.checkpoint_id,
                )
            )
        directory.head_checkpoint_id = replacement.id


async def test_initial_recomputation_after_link_removal_has_no_source_or_migration_sequence(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    阻塞初次准备的辅助链接消失后可以建立干净目标，不虚构来源或成功增量基线。

    :param prepared (RuntimeHarness): 尚未使用目标的账户
    :param tmp_path (Path): 内容卷
    """
    original = await initial_conflict(prepared, tmp_path)
    assert original.operation_id is not None
    await remove_auxiliary_link(prepared, tmp_path)
    result = await resolve(prepared, tmp_path, original.operation_id, edit_request())
    assert result.replacement_id is not None
    replacement = await info(prepared, tmp_path, result.replacement_id)
    assert replacement.status == "ready" and replacement.mode == "initial"
    assert replacement.source_state_id is None and replacement.original.migration_sequence is None
    assert not replacement.live.recomputation_reasons


async def test_older_recomputation_never_automatically_imports_newer_learning(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    旧版准备的链接冲突消失后仍从目标原始包开始，提交的 incoming 选择不跨比较执行。

    :param stopped (RuntimeHarness): 已使用旧分支账户
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"learned"})
    )
    unused = await update_version(stopped, tmp_path, "unused")
    await update_version(stopped, tmp_path, "latest")
    await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    latest = await new_session(stopped, tmp_path)
    await publish(
        latest, tmp_path, await ingest(latest, tmp_path, {}, links={"aux": "learning/memory"})
    )
    await pin(stopped, tmp_path, unused)
    original = await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    assert original.mode == "older" and original.operation_id is not None
    await remove_auxiliary_link(stopped, tmp_path)
    result = await resolve(stopped, tmp_path, original.operation_id, edit_request())
    assert result.replacement_id is not None
    replacement = await info(stopped, tmp_path, result.replacement_id)
    assert replacement.status == "ready" and replacement.mode == "older"
    assert (
        replacement.original.migration_sequence is None
        and not replacement.live.recomputation_reasons
    )
    async with stopped.database() as session:
        assert replacement.original.result_tree_digest is not None
        tree = await content_service(session, tmp_path).read_tree(
            stopped.owner, "state", replacement.original.result_tree_digest
        )
        assert "learning/memory" not in {entry.path for entry in tree.entries}
