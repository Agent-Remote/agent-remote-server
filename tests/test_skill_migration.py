"""
验证跨版本增量基线、晚到写入、目标自身改动及完整事务边界。
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import directory_tree, ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_preparation import pin, prepare, update_version
from test_skill_preparation import request as preparation_request
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute

from agent_remote_server.models.skill_preparation import (
    SkillBranchPreparation,
    SkillEffectiveBranch,
)
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, AccountSkillState
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_migration import (
    SkillMigrationRequest,
    SkillMigrationSelector,
    SkillMigrationView,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration import SkillMigrationService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def request(
    state: RuntimeHarness, root: Path, source: UUID, target: UUID
) -> SkillMigrationRequest:
    """
    独立查询双方和成功基线，构造不依赖当前 pin 的明确迁移请求。

    :param state (RuntimeHarness): 原始账户身份
    :param root (Path): 内容卷
    :param source (UUID): 来源版本
    :param target (UUID): 目标版本
    :return SkillMigrationRequest: 精确前置条件和新幂等键
    """
    async with state.database.begin() as session:
        service = SkillMigrationService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        )
        selector = SkillMigrationSelector(
            account_id=state.account,
            skill="learning",
            from_revision=str(source),
            to_revision=str(target),
        )
        expected = await service.selection.current(state.owner, selector)
        return SkillMigrationRequest(
            selector=selector, expected=expected, idempotency_key=str(uuid4())
        )


async def migrate(
    state: RuntimeHarness,
    root: Path,
    payload: SkillMigrationRequest,
    policy: SkillStoragePolicy | None = None,
) -> SkillMigrationView:
    """
    在真实独立请求事务中执行明确迁移。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 内容卷
    :param payload (SkillMigrationRequest): 已预览请求
    :param policy (SkillStoragePolicy | None): 可选测试配额
    :return SkillMigrationView: 完整已提交结果
    """
    async with state.database.begin() as session:
        return await SkillMigrationService(
            session, PrivateObjectStore(root / "objects"), policy or SkillStoragePolicy()
        ).execute(state.owner, payload)


async def versions(state: RuntimeHarness, root: Path) -> tuple[UUID, UUID]:
    """
    登记新版本但保留来源旧版的真实会话与已发布分支。

    :param state (RuntimeHarness): 旧版会话
    :param root (Path): 内容卷
    :return tuple[UUID, UUID]: 来源和目标版本
    """
    source = (await command(state, root)).expected.targets[0].revision_id
    return source, await update_version(state, root, "new upstream")


async def test_first_explicit_migration_preview_concurrency_and_replay(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    首次显式迁移以来源原始包为基线，预览无写入且并发同键只受理一次。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped,
        tmp_path,
        await ingest(stopped, tmp_path, {"learning/memory": b"learned", "aux": b"preserved"}),
    )
    source, target = await versions(stopped, tmp_path)
    payload = await request(stopped, tmp_path, source, target)
    async with stopped.database() as session:
        uploads = await session.scalar(
            select(func.count())
            .select_from(SkillContentUpload)
            .where(SkillContentUpload.user_id == stopped.owner)
        )
    preview = await migrate(stopped, tmp_path, payload.model_copy(update={"dry_run": True}))
    assert preview.base_source == "old_original" and preview.current_source == "target_original"
    assert (
        preview.status == "ready"
        and preview.operation_id is None
        and preview.migration_sequence is None
    )
    assert preview.changes is not None and [item.path for item in preview.changes] == [
        "learning/memory"
    ]
    async with stopped.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillContentUpload)
                .where(SkillContentUpload.user_id == stopped.owner)
            )
            == uploads
        )
    first, second = await asyncio.gather(
        migrate(stopped, tmp_path, payload), migrate(stopped, tmp_path, payload)
    )
    assert first == second and first.migration_sequence == 1
    assert first.result_tree_digest == preview.result_tree_digest
    assert "learning/memory" in {
        entry.path for entry in (await directory_tree(stopped, tmp_path)).entries
    }
    async with stopped.database() as session:
        ledger = await session.scalar(
            select(SkillEffectiveBranch).where(SkillEffectiveBranch.user_id == stopped.owner)
        )
        assert ledger is not None and ledger.state_id == stopped.state
    current = await request(stopped, tmp_path, source, target)
    assert current.expected.last_migrated_checkpoint_id == payload.expected.source.checkpoint_id
    assert (
        current.expected.last_sequence == 1
        and not current.expected.source_has_unmigrated_checkpoint
    )
    with pytest.raises(SkillContentError) as error:
        await migrate(
            stopped, tmp_path, payload.model_copy(update={"idempotency_key": str(uuid4())})
        )
    assert error.value.code == "STATE_PRECONDITION_CHANGED"
    with pytest.raises(SkillContentError) as error:
        await migrate(stopped, tmp_path, payload.model_copy(update={"expected": current.expected}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


async def test_automatic_migration_baseline_prevents_reintroducing_deleted_learning(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    目标主动删除已迁入文件后，旧分支新增其他文件只迁移增量而不复活删除内容。

    :param stopped (RuntimeHarness): 初始旧版会话
    :param tmp_path (Path): 私有内容卷
    """
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"old learned"})
    )
    late = await new_session(stopped, tmp_path)
    source, target = await versions(stopped, tmp_path)
    automatic = await prepare(stopped, tmp_path, await preparation_request(stopped, tmp_path))
    assert automatic.migration_sequence == 1
    newer = await new_session(stopped, tmp_path)
    await publish(
        newer,
        tmp_path,
        await ingest(
            newer, tmp_path, {"learning/memory": None, "learning/new-only": b"target work"}
        ),
    )
    await publish(
        late, tmp_path, await ingest(late, tmp_path, {"learning/late": b"late source work"})
    )
    payload = await request(stopped, tmp_path, source, target)
    assert payload.expected.last_migration_id == automatic.operation_id
    assert payload.expected.source_has_unmigrated_checkpoint
    result = await migrate(stopped, tmp_path, payload)
    assert result.status == "ready" and result.migration_sequence == 2
    assert result.base_source == "last_migrated" and result.current_source == "target_published"
    assert result.changes is not None and [entry.path for entry in result.changes] == [
        "learning/late"
    ]
    paths = {entry.path for entry in (await directory_tree(stopped, tmp_path)).entries}
    assert "learning/memory" not in paths and {"learning/late", "learning/new-only"} <= paths
    repeat = await migrate(stopped, tmp_path, await request(stopped, tmp_path, source, target))
    assert repeat.status == "ready" and repeat.migration_sequence == 3 and repeat.changes == []
    assert (
        repeat.directory_changes == []
        and repeat.result_checkpoint_id == result.result_checkpoint_id
    )
    assert await migrate(stopped, tmp_path, payload) == result


async def test_conflict_does_not_advance_baseline_and_reset_starts_new_epoch(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    双侧后来修改同一路径保留冲突，重置目标后新显式迁移不复用旧纪元基线。

    :param stopped (RuntimeHarness): 原始旧会话
    :param tmp_path (Path): 私有内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"one"}))
    late = await new_session(stopped, tmp_path)
    source, target = await versions(stopped, tmp_path)
    first = await migrate(stopped, tmp_path, await request(stopped, tmp_path, source, target))
    newer = await new_session(stopped, tmp_path)
    await publish(newer, tmp_path, await ingest(newer, tmp_path, {"learning/memory": b"target"}))
    await publish(late, tmp_path, await ingest(late, tmp_path, {"learning/memory": b"source"}))
    payload = await request(stopped, tmp_path, source, target)
    before = await directory_tree(stopped, tmp_path)
    conflict = await migrate(stopped, tmp_path, payload)
    assert conflict.status == "conflicted" and conflict.migration_sequence is None
    assert conflict.changes is None and conflict.result_checkpoint_id is None
    assert await directory_tree(stopped, tmp_path) == before
    current = await request(stopped, tmp_path, source, target)
    assert (
        current.expected.last_migration_id == first.operation_id
        and current.expected.last_sequence == 1
    )
    reset = await state_execute(stopped, tmp_path, await command(stopped, tmp_path))
    assert reset.superseded_conflicts == 1
    async with stopped.database() as session:
        row = await session.get(SkillBranchPreparation, conflict.operation_id)
        assert row is not None and row.status == "superseded" and row.migration_sequence is None
    current = await request(stopped, tmp_path, source, target)
    assert (
        current.expected.last_sequence == 0 and current.expected.last_migrated_checkpoint_id is None
    )
    recovered = await migrate(stopped, tmp_path, current)
    assert recovered.status == "ready" and recovered.migration_sequence == 1
    assert recovered.base_source == "old_original"
    assert await migrate(stopped, tmp_path, payload) == conflict


async def test_source_only_change_after_preview_invalidates_whole_request(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    预览后的来源 checkpoint 变化必须重新预览，不能把更多未确认变化合入目标。

    :param stopped (RuntimeHarness): 已使用来源会话
    :param tmp_path (Path): 内容卷
    """
    source, target = await versions(stopped, tmp_path)
    payload = await request(stopped, tmp_path, source, target)
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/late": b"later"}))
    with pytest.raises(SkillContentError) as error:
        await migrate(stopped, tmp_path, payload)
    assert error.value.code == "STATE_PRECONDITION_CHANGED"
    assert (await request(stopped, tmp_path, source, target)).expected.target.state_id is None


async def test_non_effective_target_and_reverse_migration_do_not_change_rules(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    显式目标无需等于 pin，反向迁移仍比较目标自己状态而不改用户规则。

    :param stopped (RuntimeHarness): 最初来源
    :param tmp_path (Path): 私有内容卷
    """
    source, target = await versions(stopped, tmp_path)
    await pin(stopped, tmp_path, source)
    before = await command(stopped, tmp_path)
    first = await migrate(stopped, tmp_path, await request(stopped, tmp_path, source, target))
    assert first.status == "ready"
    assert (await command(stopped, tmp_path)).expected.targets == before.expected.targets
    await pin(stopped, tmp_path, target)
    newer = await new_session(stopped, tmp_path)
    await publish(
        newer, tmp_path, await ingest(newer, tmp_path, {"learning/new-knowledge": b"back to old"})
    )
    before = await command(stopped, tmp_path)
    result = await migrate(stopped, tmp_path, await request(stopped, tmp_path, target, source))
    assert result.status == "ready" and result.before.last_sequence == 0
    after = await command(stopped, tmp_path)
    assert after.expected.targets == before.expected.targets
    assert after.expected.library_generation == before.expected.library_generation
    assert "learning/new-knowledge" in {
        entry.path for entry in (await directory_tree(stopped, tmp_path)).entries
    }


async def test_last_cas_failure_rolls_back_success_sequence(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    最后目录交换失败，即使外层继续提交也不能留下成功序号、目标分支或上传。

    :param stopped (RuntimeHarness): 旧版会话
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 最后 CAS 故障注入
    """
    source, target = await versions(stopped, tmp_path)
    payload = await request(stopped, tmp_path, source, target)
    async with stopped.database() as session:
        uploads = await session.scalar(
            select(func.count())
            .select_from(SkillContentUpload)
            .where(SkillContentUpload.user_id == stopped.owner)
        )

    async def reject(
        self: SkillPublicationRepository, directory: AccountSkillDirectoryState, checkpoint_id: UUID
    ) -> bool:
        """
        模拟完整目录 CAS 竞争失败。

        :param directory (AccountSkillDirectoryState): 原始目录
        :param checkpoint_id (UUID): 待发布检查点
        :return bool: 本次交换失败
        """
        return False

    monkeypatch.setattr(SkillPublicationRepository, "advance_directory", reject)
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError, match="directory changed"):
            await SkillMigrationService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).execute(stopped.owner, payload)
    assert (await request(stopped, tmp_path, source, target)).expected == payload.expected
    async with stopped.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillContentUpload)
                .where(SkillContentUpload.user_id == stopped.owner)
            )
            == uploads
        )
        assert (
            await session.scalar(
                select(SkillBranchPreparation).where(
                    SkillBranchPreparation.user_id == stopped.owner
                )
            )
            is None
        )


@pytest.mark.parametrize("kind", ["expired", "quota", "linked", "opaque"])
async def test_unsafe_or_unavailable_migration_never_partially_publishes(
    stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    过期或超额不写入，不透明和关联状态保存冲突而不发布部分目录。

    :param stopped (RuntimeHarness): 旧版会话
    :param tmp_path (Path): 内容卷
    :param kind (str): 不可直接合入的状态种类
    """
    if kind == "linked":
        await publish(
            stopped,
            tmp_path,
            await ingest(stopped, tmp_path, {"aux": b"shared"}, links={"learning/link": "../aux"}),
        )
    elif kind == "opaque":
        await publish(
            stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/state.db": b"db"})
        )
    elif kind == "expired":
        async with stopped.database.begin() as session:
            branch = await session.get(AccountSkillState, stopped.state)
            assert branch is not None
            branch.expired = True
    source, target = await versions(stopped, tmp_path)
    payload = await request(stopped, tmp_path, source, target)
    before = await directory_tree(stopped, tmp_path)
    if kind in {"expired", "quota"}:
        with pytest.raises(SkillContentError) as error:
            await migrate(
                stopped,
                tmp_path,
                payload.model_copy(update={"dry_run": True}),
                SkillStoragePolicy(user_state_bytes=1) if kind == "quota" else None,
            )
        assert error.value.code == ("STATE_EXPIRED" if kind == "expired" else "QUOTA_EXCEEDED")
    else:
        result = await migrate(stopped, tmp_path, payload)
        assert result.status == "conflicted" and result.migration_sequence is None
        assert result.conflicts[0].reason == (
            "source_conflict" if kind == "linked" else "opaque_divergence"
        )
    assert await directory_tree(stopped, tmp_path) == before


async def test_missing_retained_delta_baseline_never_replays_original_difference(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    先前成功基线若已不可读取必须明确失败，不能退回原始包重复套用所有旧差异。

    :param stopped (RuntimeHarness): 初始旧版会话
    :param tmp_path (Path): 私有内容卷
    """
    from agent_remote_server.models.skill_state import SkillCheckpoint

    source, target = await versions(stopped, tmp_path)
    first_request = await request(stopped, tmp_path, source, target)
    await migrate(stopped, tmp_path, first_request)
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/late": b"new source head"})
    )
    async with stopped.database.begin() as session:
        checkpoint = await session.get(SkillCheckpoint, first_request.expected.source.checkpoint_id)
        assert checkpoint is not None
        checkpoint.retained = False
        checkpoint.tree_digest = None
    payload = await request(stopped, tmp_path, source, target)
    with pytest.raises(SkillContentError) as error:
        await migrate(stopped, tmp_path, payload)
    assert error.value.code == "STATE_EXPIRED"
    assert (await request(stopped, tmp_path, source, target)).expected.last_sequence == 1


async def test_explicit_revisions_must_be_distinct_and_belong_to_same_source(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    相同版本或其他技能的版本身份不能伪装成该技能的显式迁移目标。

    :param stopped (RuntimeHarness): 原始来源
    :param tmp_path (Path): 私有内容卷
    """
    from test_skill_library import LibraryHarness

    source, _ = await versions(stopped, tmp_path)
    with pytest.raises(SkillContentError) as error:
        await request(stopped, tmp_path, source, source)
    assert error.value.code == "INVALID_REQUEST"
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.add(await library.candidate(name="other"))
    other = (await library.info("other")).default_revision_id
    assert other is not None
    with pytest.raises(SkillContentError) as error:
        await request(stopped, tmp_path, source, other)
    assert error.value.code == "REVISION_NOT_FOUND"
