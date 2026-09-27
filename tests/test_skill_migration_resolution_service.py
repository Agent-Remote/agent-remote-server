"""
验证迁移解决的完整关联发布、成功基线、逐分支预览和整个保存点回滚。
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_local import activate, register
from test_skill_local import source as local_source
from test_skill_migration import migrate, request, versions
from test_skill_migration_conflicts import counts, pending
from test_skill_migration_related_sources import linked_sources
from test_skill_migration_resolution_drafts import edit, edit_request, saved_plan, two_conflicts
from test_skill_migration_resolution_plan import calculate
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute

from agent_remote_server.models.skill_migration_resolution import SkillMigrationResolutionOperation
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.repositories.skill_migration_resolution import (
    SkillMigrationResolutionRepository,
)
from agent_remote_server.repositories.skill_preparation import SkillPreparationRepository
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_migration_resolution import SkillMigrationResolutionView
from agent_remote_server.schemas.skill_resolution import (
    SkillResolutionChoice,
    SkillResolutionRequest,
    SkillResolutionUploadRequest,
)
from agent_remote_server.services.skills.branch_publication import SkillBranchPublisher
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_conflicts import SkillMigrationConflictService
from agent_remote_server.services.skills.migration_resolution import SkillMigrationResolutionService
from agent_remote_server.services.skills.migration_resolution_content import (
    SkillMigrationResolutionContentService,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def resolve(
    state: RuntimeHarness, root: Path, identity: UUID, payload: SkillResolutionRequest
) -> SkillMigrationResolutionView:
    """
    使用独立真实请求事务验证幂等和发布结果可以跨服务重建恢复。

    :param state (RuntimeHarness): 原账户
    :param root (Path): 私有卷
    :param identity (UUID): 原迁移冲突
    :param payload (SkillResolutionRequest): 完整明确选择
    :return SkillMigrationResolutionView: 已提交的最终解决结果
    """
    async with state.database.begin() as session:
        return await SkillMigrationResolutionService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).execute(state.owner, identity, payload)


@pytest.mark.parametrize("mode", ["forward", "incremental"])
@pytest.mark.parametrize("side", ["current", "incoming"])
async def test_preview_atomic_publish_and_original_receipt_preservation(
    stopped: RuntimeHarness, tmp_path: Path, mode: str, side: str
) -> None:
    """
    首次和增量冲突都可完整解决，预览无写入，成功更新独立字段而不改写原响应。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param mode (str): 原迁移模式
    :param side (str): 完整侧选择
    """
    original = await pending(stopped, tmp_path, mode)
    identity = original.operation_id
    assert identity is not None
    payload = edit_request(SkillResolutionChoice.model_validate({"use": side}))
    rows = await counts(stopped)
    before = (await command(stopped, tmp_path)).expected
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None
        usage_before = (usage.lock_version, usage.state_bytes, usage.state_reserved)
    preview = await resolve(
        stopped, tmp_path, identity, payload.model_copy(update={"dry_run": True})
    )
    assert preview.status == "preview" and preview.candidate_complete and preview.plan_revision == 0
    assert (
        preview.operation_id is None
        and preview.result_checkpoint_id is None
        and preview.migration_sequence is None
    )
    assert len(preview.affected) == 1 and preview.affected[0].modified == (side == "incoming")
    assert preview.target_modified == (side == "incoming")
    assert preview.affected[0].original_changes == preview.original_changes
    assert await counts(stopped) == rows and await saved_plan(stopped, identity) == (0, [], 0)
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert (
            usage is not None
            and (usage.lock_version, usage.state_bytes, usage.state_reserved) == usage_before
        )
    result = await resolve(stopped, tmp_path, identity, payload)
    assert (
        result.status == "published"
        and result.migration_sequence == 1
        and result.plan_revision == 1
    )
    assert result.result_tree_digest == preview.result_tree_digest
    assert result.result_checkpoint_id == result.affected[0].result_checkpoint_id
    assert await resolve(stopped, tmp_path, identity, payload) == result
    async with stopped.database.begin() as session:
        row = await session.get(SkillBranchPreparation, identity)
        assert row is not None and row.status == "ready" and row.migration_sequence == 1
        assert row.response_json == original.model_dump(mode="json")
        assert (
            row.result_checkpoint_id == result.result_checkpoint_id
            and row.result_directory_id == result.result_directory_id
        )
        target = await session.get(AccountSkillState, row.target_state_id)
        assert target is not None and target.head_checkpoint_id == result.result_checkpoint_id
        assert target.epoch == row.target_epoch
        checkpoint = await session.get(SkillCheckpoint, result.result_checkpoint_id)
        assert (
            checkpoint is not None and checkpoint.backing_directory_id == result.result_directory_id
        )
        info = await SkillMigrationConflictService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).info(stopped.owner, identity)
        assert (
            info.original == original
            and info.status == "ready"
            and not info.live.recomputation_reasons
        )
        receipt = await SkillMigrationResolutionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).receipt(stopped.owner, payload.idempotency_key)
        assert receipt.result == result and receipt.current_status == "ready"
    after = (await command(stopped, tmp_path)).expected
    assert (
        after.library_generation == before.library_generation
        and after.directory_epoch == before.directory_epoch
    )
    if side == "current":
        await new_session(stopped, tmp_path)


async def test_partial_plan_publishes_only_after_last_choice_and_replays_pending_receipt(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    每条路径先保存计划，全部解决才发布；旧 pending 回执不能重放成新发布或倒退基线。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    identity = await two_conflicts(stopped, tmp_path)
    before = (await command(stopped, tmp_path)).expected
    rows = await counts(stopped)
    first_request = edit_request(SkillResolutionChoice(path="learning/a", use="incoming"))
    first = await resolve(stopped, tmp_path, identity, first_request)
    assert first.status == "pending" and not first.candidate_complete and not first.affected
    assert first.migration_sequence is None and first.result_directory_id is None
    assert (await command(stopped, tmp_path)).expected == before and await counts(stopped) == rows
    second_request = edit_request(SkillResolutionChoice(path="learning/b", use="current"), 1)
    result = await resolve(stopped, tmp_path, identity, second_request)
    assert (
        result.status == "published"
        and result.migration_sequence == 2
        and result.plan_revision == 2
    )
    assert [change.path for change in result.target_changes or []] == ["learning/a"]
    assert await resolve(stopped, tmp_path, identity, first_request) == first
    async with stopped.database.begin() as session:
        service = SkillMigrationResolutionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        receipt = await service.receipt(stopped.owner, first_request.idempotency_key)
        assert receipt.result.status == "pending" and receipt.current_status == "ready"
        row = await session.get(SkillBranchPreparation, identity)
        assert row is not None
        original = SkillMigrationView.model_validate(row.response_json)
        assert row.source_checkpoint_id == original.before.source.checkpoint_id
    again = await migrate(
        stopped,
        tmp_path,
        await request(
            stopped,
            tmp_path,
            original.before.source.revision_id,
            original.before.target.revision_id,
        ),
    )
    assert again.before.last_migration_id == identity
    assert again.before.last_migrated_checkpoint_id == original.before.source.checkpoint_id
    assert (
        again.result_checkpoint_id == result.result_checkpoint_id
        and again.result_directory_id == result.result_directory_id
    )


@pytest.mark.parametrize("origin", ["user_library", "account_local"])
async def test_linked_actual_changes_publish_all_branches_and_preview_own_baselines(
    stopped: RuntimeHarness, tmp_path: Path, origin: str
) -> None:
    """
    关联库或本地来源与目标一起发布，预览完整列出各自原始包覆盖与当前状态变化。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param origin (str): 关联来源种类
    """
    if origin == "user_library":
        original = await linked_sources(stopped, tmp_path, "head")
    else:
        local = await register(stopped, tmp_path, await local_source(stopped, tmp_path))
        await activate(stopped, local)
        active = await new_session(stopped, tmp_path)
        await publish(
            active,
            tmp_path,
            await ingest(active, tmp_path, {}, links={"learning/link": "../notes/SKILL.md"}),
        )
        newer = await new_session(stopped, tmp_path)
        await publish(
            newer, tmp_path, await ingest(newer, tmp_path, {"notes/memory": b"new context"})
        )
        source_revision, target_revision = await versions(stopped, tmp_path)
        original = await migrate(
            stopped, tmp_path, await request(stopped, tmp_path, source_revision, target_revision)
        )
    identity = original.operation_id
    assert identity is not None and original.status == "conflicted"
    payload = edit_request()
    preview = await resolve(
        stopped, tmp_path, identity, payload.model_copy(update={"dry_run": True})
    )
    views = {item.name: item for item in preview.affected}
    assert set(views) == {"learning", "notes"} and views["notes"].origin == origin
    assert [change.path for change in views["notes"].changes] == ["notes/memory"]
    assert views["notes"].original_changes == [] and not views["notes"].modified
    result = await resolve(stopped, tmp_path, identity, payload)
    assert result.status == "published" and result.other_changed_roots == ("notes",)
    async with stopped.database() as session:
        for item in result.affected:
            state = await session.get(AccountSkillState, item.state_id)
            checkpoint = await session.get(SkillCheckpoint, item.result_checkpoint_id)
            member = await session.get(
                SkillDirectoryMember, (result.result_directory_id, item.name)
            )
            assert state is not None and checkpoint is not None and member is not None
            assert state.head_checkpoint_id == checkpoint.id == member.checkpoint_id
            assert checkpoint.parent_id == views[item.name].checkpoint_id
            assert checkpoint.backing_directory_id == result.result_directory_id
            assert checkpoint.state_epoch == item.state_epoch == state.epoch
            assert (state.base_revision_id or state.local_revision_id) == item.revision_id
        source = await session.get(AccountSkillState, original.before.source.state_id)
        assert (
            source is not None and source.head_checkpoint_id == original.before.source.checkpoint_id
        )


async def test_unchanged_related_member_keeps_original_checkpoint(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    相同关联内容不算写入，不能仅因参与连通单元就改写其 head 或 provenance。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    original = await linked_sources(stopped, tmp_path, "none")
    assert original.operation_id is not None
    async with stopped.database() as session:
        old = await session.get(
            SkillDirectoryMember, (original.before.directory_checkpoint_id, "notes")
        )
        assert old is not None
        old_checkpoint = old.checkpoint_id
    result = await resolve(stopped, tmp_path, original.operation_id, edit_request())
    assert [item.name for item in result.affected] == ["learning"]
    async with stopped.database() as session:
        member = await session.get(SkillDirectoryMember, (result.result_directory_id, "notes"))
        assert member is not None and member.checkpoint_id == old_checkpoint


async def test_custom_linked_deletion_publishes_empty_view_without_member(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    人工目录明确删除关联 skill 时保留其同身份空视图，不能留下指向旧内容的成员。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    original = await linked_sources(stopped, tmp_path, "head")
    identity = original.operation_id
    assert identity is not None
    current = await calculate(stopped, tmp_path, identity, [SkillResolutionChoice(use="current")])
    async with stopped.database.begin() as session:
        service = SkillMigrationResolutionContentService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        upload = await service.begin(
            stopped.owner,
            identity,
            SkillResolutionUploadRequest(
                idempotency_key=str(uuid4()), manifest=current.inputs.current
            ),
        )
        tree = await service.complete(stopped.owner, identity, upload.id)
        digest = tree.digest
    result = await resolve(
        stopped,
        tmp_path,
        identity,
        edit_request(SkillResolutionChoice(directory_tree_digest=digest)),
    )
    assert result.status == "published"
    notes = next(item for item in result.affected if item.name == "notes")
    assert notes.modified and notes.original_changes
    async with stopped.database() as session:
        assert (
            await session.get(SkillDirectoryMember, (result.result_directory_id, "notes")) is None
        )
        checkpoint = await session.get(SkillCheckpoint, notes.result_checkpoint_id)
        state = await session.get(AccountSkillState, notes.state_id)
        assert checkpoint is not None and state is not None
        assert state.head_checkpoint_id == checkpoint.id and checkpoint.invalid_skill_format
        assert checkpoint.backing_directory_id == result.result_directory_id


@pytest.mark.parametrize("failure", ["second-branch", "directory", "migration", "receipt"])
async def test_late_failure_rolls_back_every_branch_plan_baseline_and_receipt(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """
    多分支发布最后阶段失败，外层捕获并提交也不能留下半次发布或提前推进的基线。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param monkeypatch (pytest.MonkeyPatch): 最后阶段故障注入
    :param failure (str): 拒绝提交的步骤
    """
    original = await linked_sources(stopped, tmp_path, "head")
    identity = original.operation_id
    assert identity is not None
    before = (await command(stopped, tmp_path, directory=True)).expected
    rows = await counts(stopped)
    original_advance = SkillPublicationRepository.advance_branch
    original_receipt = SkillMigrationResolutionRepository.save_operation
    advances = 0

    async def fail_second(
        self: SkillPublicationRepository, branch: AccountSkillState, checkpoint_id: UUID
    ) -> bool:
        """
        第二个分支拒绝 CAS，第一次实际成功也必须回滚。

        :param branch (AccountSkillState): 已验证分支
        :param checkpoint_id (UUID): 新检查点
        :return bool: 是否接受此次 CAS
        """
        nonlocal advances
        advances += 1
        return await original_advance(self, branch, checkpoint_id) if advances == 1 else False

    async def fail_directory(
        self: SkillPublicationRepository, directory: AccountSkillDirectoryState, checkpoint_id: UUID
    ) -> bool:
        """
        所有分支已写入后拒绝完整目录交换。

        :param directory (AccountSkillDirectoryState): 原目录
        :param checkpoint_id (UUID): 新目录
        :return bool: 始终拒绝
        """
        return False

    async def fail_migration(
        self: SkillPreparationRepository,
        row: SkillBranchPreparation,
        checkpoint_id: UUID,
        directory_id: UUID,
        sequence: int | None,
    ) -> bool:
        """
        全部 head 已交换后拒绝迁移记录状态更新。

        :param row (SkillBranchPreparation): 原始迁移
        :param checkpoint_id (UUID): 已生成目标
        :param directory_id (UUID): 已生成目录
        :param sequence (int | None): 待写入成功序号，非迁移准备为空
        :return bool: 始终拒绝
        """
        return False

    async def fail_receipt(
        self: SkillMigrationResolutionRepository, operation: SkillMigrationResolutionOperation
    ) -> None:
        """
        原响应已刷新后故障，覆盖所有嵌套保存点均已释放的情形。

        :param operation (SkillMigrationResolutionOperation): 最终不可变回执
        """
        await original_receipt(self, operation)
        raise RuntimeError("late receipt failure")

    if failure == "second-branch":
        monkeypatch.setattr(SkillPublicationRepository, "advance_branch", fail_second)
    elif failure == "directory":
        monkeypatch.setattr(SkillPublicationRepository, "advance_directory", fail_directory)
    elif failure == "migration":
        monkeypatch.setattr(SkillPreparationRepository, "resolve", fail_migration)
    else:
        monkeypatch.setattr(SkillMigrationResolutionRepository, "save_operation", fail_receipt)
    async with stopped.database.begin() as session:
        service = SkillMigrationResolutionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        with pytest.raises((SkillContentError, RuntimeError)):
            await service.execute(stopped.owner, identity, edit_request())
    assert await saved_plan(stopped, identity) == (0, [], 0)
    assert await counts(stopped) == rows
    assert (await command(stopped, tmp_path, directory=True)).expected == before
    async with stopped.database() as session:
        row = await session.get(SkillBranchPreparation, identity)
        assert row is not None and row.status == "conflicted" and row.migration_sequence is None
        assert row.response_json == original.model_dump(mode="json")


async def test_concurrent_duplicate_only_publishes_once(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    同键并发只接受一个新版本和一次全目录发布，断线重试不能再次推进基线。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    original = await pending(stopped, tmp_path)
    assert original.operation_id is not None
    payload = edit_request(SkillResolutionChoice(use="current"))
    first, second = await asyncio.gather(
        resolve(stopped, tmp_path, original.operation_id, payload),
        resolve(stopped, tmp_path, original.operation_id, payload),
    )
    assert first == second and first.status == "published"
    assert (await saved_plan(stopped, original.operation_id))[0::2] == (1, 1)


async def test_newer_success_cannot_be_rewound_by_older_retained_conflict(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    另一次成功迁移使原比较失效，旧输入不能倒退目标或成功基线。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    original = await pending(stopped, tmp_path)
    assert isinstance(original, SkillMigrationView) and original.operation_id is not None
    newer = await migrate(
        stopped,
        tmp_path,
        await request(
            stopped,
            tmp_path,
            original.before.source.revision_id,
            original.before.target.revision_id,
        ),
    )
    assert newer.operation_id is not None and newer.status == "conflicted"
    result = await resolve(
        stopped, tmp_path, newer.operation_id, edit_request(SkillResolutionChoice(use="current"))
    )
    before = (await command(stopped, tmp_path)).expected
    superseded = await resolve(stopped, tmp_path, original.operation_id, edit_request())
    assert superseded.status == "superseded" and superseded.replacement_id == newer.operation_id
    assert not superseded.recomputation_possible
    assert (await command(stopped, tmp_path)).expected == before
    assert await saved_plan(stopped, original.operation_id) == (0, [], 1)
    async with stopped.database() as session:
        old = await session.get(SkillBranchPreparation, original.operation_id)
        accepted = await session.get(SkillBranchPreparation, newer.operation_id)
        assert (
            old is not None and old.migration_sequence is None and old.result_checkpoint_id is None
        )
        assert (
            accepted is not None and accepted.migration_sequence == result.migration_sequence == 1
        )


@pytest.mark.parametrize("dry_run", [False, True])
async def test_sequence_exhaustion_rejected_before_any_plan_or_publication(
    stopped: RuntimeHarness, tmp_path: Path, dry_run: bool
) -> None:
    """
    预览同样检查成功序号上限，不能给出实际无法提交的完整结果。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param dry_run (bool): 是否预览
    """
    identity = await two_conflicts(stopped, tmp_path)
    async with stopped.database.begin() as session:
        conflict = await session.get(SkillBranchPreparation, identity)
        assert conflict is not None
        original = SkillMigrationView.model_validate(conflict.response_json)
        previous = await session.get(SkillBranchPreparation, original.before.last_migration_id)
        assert previous is not None
        previous.migration_sequence = 2**63 - 1
    before = (await command(stopped, tmp_path)).expected
    rows = await counts(stopped)
    with pytest.raises(SkillContentError) as error:
        await resolve(stopped, tmp_path, identity, edit_request(dry_run=dry_run))
    assert error.value.code == "LIMIT_EXCEEDED"
    assert await saved_plan(stopped, identity) == (0, [], 0)
    assert (await command(stopped, tmp_path)).expected == before and await counts(stopped) == rows


async def test_owner_keys_typed_receipts_and_replay_after_reset(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    草稿和最终回执不可混读，其他用户及变更请求不可借旧键授权，reset 后重放不再执行。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    original = await pending(stopped, tmp_path)
    identity = original.operation_id
    assert identity is not None
    draft_request = edit_request()
    await edit(stopped, tmp_path, identity, draft_request)
    payload = edit_request(SkillResolutionChoice(use="current"), 1)
    result = await resolve(stopped, tmp_path, identity, payload)
    other = await user(stopped.database)
    async with stopped.database.begin() as session:
        service = SkillMigrationResolutionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        with pytest.raises(SkillContentError) as error:
            await service.receipt(stopped.owner, draft_request.idempotency_key)
        assert error.value.code == "OPERATION_KIND_MISMATCH"
        with pytest.raises(SkillContentError) as error:
            await service.receipt(other, payload.idempotency_key)
        assert error.value.code == "OPERATION_NOT_FOUND"
        with pytest.raises(SkillContentError) as error:
            await service.execute(other, identity, payload)
        assert error.value.code == "CONFLICT_NOT_FOUND"
    with pytest.raises(SkillContentError) as error:
        await resolve(
            stopped,
            tmp_path,
            identity,
            payload.model_copy(update={"choice": SkillResolutionChoice(use="incoming")}),
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path))
    before = (await command(stopped, tmp_path)).expected
    assert await resolve(stopped, tmp_path, identity, payload) == result
    assert (await command(stopped, tmp_path)).expected == before
    async with stopped.database() as session:
        assert await session.get(SkillStorageUsage, other) is None


async def test_single_target_publication_refuses_hidden_related_content_changes(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    单分支调用不能把另一个关联成员的实际变化夹带进目录却保留其旧引用。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    original = await linked_sources(stopped, tmp_path, "head")
    assert original.operation_id is not None
    before = (await command(stopped, tmp_path, directory=True)).expected
    rows = await counts(stopped)
    async with stopped.database.begin() as session:
        service = SkillMigrationResolutionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        await service.queries.library.lock_library(stopped.owner)
        migration = await service.planner.conflicts.require(stopped.owner, original.operation_id)
        calculation = await service.planner.calculate(
            stopped.owner, original.operation_id, [SkillResolutionChoice(use="incoming")]
        )
        publication = await service._apply.prepare(migration, calculation)
        with pytest.raises(SkillContentError) as error:
            await SkillBranchPublisher(
                service.queries,
                SkillPublicationRepository(session),
                PrivateObjectStore(tmp_path / "objects"),
            ).publish(publication.target, publication.plan, str(uuid4()))
        assert error.value.code == "STATE_SCOPE_MISMATCH"
    assert (await command(stopped, tmp_path, directory=True)).expected == before
    assert await counts(stopped) == rows


async def test_auxiliary_scope_cannot_smuggle_a_new_untracked_skill_identity(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    关联辅助范围不能把新增有效 skill 当普通辅助根发布，绕过稳定身份和成员引用。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    original = await pending(stopped, tmp_path, linked=True)
    identity = original.operation_id
    assert identity is not None
    current = await calculate(stopped, tmp_path, identity, [SkillResolutionChoice(use="current")])
    candidate = await LibraryHarness(stopped.database, tmp_path, stopped.owner).candidate(
        name="unexpected"
    )
    async with stopped.database.begin() as session:
        service = SkillMigrationResolutionContentService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        package = await service.content.read_tree(stopped.owner, "package", candidate.tree_digest)
        result = SkillTreeManifest(
            entries=tuple(
                sorted(
                    (
                        *current.inputs.incoming.entries,
                        SkillTreeEntry(path="unexpected", kind="directory", mode=0o755),
                        *(
                            entry.model_copy(update={"path": "unexpected/" + entry.path})
                            for entry in package.entries
                        ),
                    ),
                    key=lambda entry: entry.path.encode(),
                )
            )
        )
        upload = await service.begin(
            stopped.owner,
            identity,
            SkillResolutionUploadRequest(idempotency_key=str(uuid4()), manifest=result),
        )
        digest = (await service.complete(stopped.owner, identity, upload.id)).digest
    before = (await command(stopped, tmp_path)).expected
    with pytest.raises(SkillContentError) as error:
        await resolve(
            stopped,
            tmp_path,
            identity,
            edit_request(SkillResolutionChoice(directory_tree_digest=digest)),
        )
    assert error.value.code == "STATE_SCOPE_MISMATCH"
    assert await saved_plan(stopped, identity) == (0, [], 0)
    assert (await command(stopped, tmp_path)).expected == before
