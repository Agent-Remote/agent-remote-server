"""
验证完整历史依赖退役、分支过期恢复和保存点回滚，不以共享文件存在替代恢复资格。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_history_retirement import retire
from test_skill_migration import migrate, versions
from test_skill_migration import request as migration_request
from test_skill_preparation import pin, prepare
from test_skill_preparation import request as preparation_request
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command, execute

from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_state import AccountSkillState, SkillCheckpoint
from agent_remote_server.models.skill_storage import SkillStorageUsage, SkillStoredTree
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention import SkillRetentionInspector
from agent_remote_server.services.skills.retention.clocks import history_records, retention_mutation
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def historical_selection(
    state: RuntimeHarness, root: Path
) -> tuple[tuple[RetentionKey, ...], UUID, UUID]:
    """
    真实发布旧版学习数据，再显式重置新版，取得已解除保护的完整历史集合。

    :param state (RuntimeHarness): 原始账户及会话
    :param root (Path): 内容卷
    :return tuple[tuple[RetentionKey, ...], UUID, UUID]: 精确历史选择、旧版 head 和旧包版本
    """
    await publish(state, root, await ingest(state, root, {"learning/memory": b"learned"}))
    old = (await command(state, root)).expected.targets[0]
    assert old.head_checkpoint_id is not None
    await versions(state, root)
    await execute(state, root, await command(state, root))
    async with state.database() as session:
        inspector = await SkillRetentionInspector(session).inspect(state.owner)
        index = await SkillRetentionRepository(session).load(state.owner)
        selected = tuple(
            sorted(
                key
                for key in history_records(index)
                if key.kind in {"checkpoint", "snapshot", "finalization", "publication"}
                and not inspector.reasons(key.kind, key.identity)
            )
        )
    assert RetentionKey("checkpoint", str(old.head_checkpoint_id)) in selected
    return selected, old.head_checkpoint_id, old.revision_id


async def test_checkpoint_retirement_deadline_audit_and_explicit_reset(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    已到期完整历史保留摘要和原 head，过期旧版本再次选中不能自动初始化，显式 reset 可恢复。

    :param stopped (RuntimeHarness): 真实原始会话
    :param tmp_path (Path): 私有内容卷
    """
    selected, head_id, revision_id = await historical_selection(stopped, tmp_path)
    with pytest.raises(SkillContentError) as error:
        await retire(stopped, *selected)
    assert error.value.code == "HISTORY_NOT_EXPIRED"
    async with stopped.database.begin() as session:
        head = await session.get(SkillCheckpoint, head_id)
        branch = await session.get(AccountSkillState, stopped.state)
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert head is not None and branch is not None and usage is not None
        original = (
            head.content_digest,
            head.parent_id,
            head.backing_directory_id,
            head.state_epoch,
        )
        original_epoch = branch.epoch
        original_usage = (usage.state_bytes, usage.package_bytes)
        index = await SkillRetentionRepository(session).load(stopped.owner)
        records = history_records(index)
        for key in selected:
            records[key].retention_released_at = datetime.now(UTC) - timedelta(days=31)
    assert await retire(stopped, *selected) == selected
    assert await retire(stopped, *selected) == ()
    async with stopped.database() as session:
        head = await session.get(SkillCheckpoint, head_id)
        branch = await session.get(AccountSkillState, stopped.state)
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert head is not None and branch is not None and usage is not None
        assert (
            head.content_digest,
            head.parent_id,
            head.backing_directory_id,
            head.state_epoch,
        ) == original
        assert not head.retained and head.tree_digest is None
        assert (
            branch.head_checkpoint_id == head_id
            and branch.expired
            and branch.epoch == original_epoch
        )
        assert (usage.state_bytes, usage.package_bytes) == original_usage
        assert (
            await session.get(SkillStoredTree, (stopped.owner, "state", head.content_digest))
            is not None
        )
        queries = SkillStateQueryService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        info = await queries.view(head)
        assert info.storage_location == "expired" and not info.retained and info.is_head
        assert head.backing_directory_id is not None
        members = await queries.members(stopped.owner, head.backing_directory_id)
        assert any(item.checkpoint_id == head_id for item in members.items)
        with pytest.raises(SkillContentError) as error:
            await queries.tree(stopped.owner, head_id)
        assert error.value.code == "STATE_EXPIRED"
    await pin(stopped, tmp_path, revision_id)
    expired = (await command(stopped, tmp_path)).expected.targets[0]
    assert expired.expired and expired.head_checkpoint_id == head_id
    with pytest.raises(SkillContentError) as error:
        await prepare(stopped, tmp_path, await preparation_request(stopped, tmp_path))
    assert error.value.code == "STATE_EXPIRED"
    with pytest.raises(SkillContentError) as error:
        await new_session(stopped, tmp_path)
    assert error.value.code == "STATE_EXPIRED"
    with pytest.raises(SkillContentError) as error:
        await execute(stopped, tmp_path, await command(stopped, tmp_path, checkpoint=head_id))
    assert error.value.code == "STATE_EXPIRED"
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    reset = (await command(stopped, tmp_path)).expected.targets[0]
    assert not reset.expired and reset.head_checkpoint_id != head_id
    assert reset.state_epoch == original_epoch + 1
    async with stopped.database() as session:
        assert reset.head_checkpoint_id is not None
        tree = await SkillStateQueryService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).tree(stopped.owner, reset.head_checkpoint_id)
        assert "learning/memory" not in {entry.path for entry in tree.manifest.entries}


async def test_checkpoint_retirement_blocks_current_directory_members(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    当前 head 不能提前回收；旧分支解除硬根后，当前目录物化义务仍禁止单独退役。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    publication = await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    current = (await command(stopped, tmp_path)).expected
    head_id = current.targets[0].head_checkpoint_id
    assert head_id is not None and current.directory_head_id is not None
    head = RetentionKey("checkpoint", str(head_id))
    directory = RetentionKey("checkpoint", str(current.directory_head_id))
    with pytest.raises(SkillContentError) as error:
        await retire(stopped, head, early=True)
    assert error.value.code == "STATE_PROTECTED"
    await versions(stopped, tmp_path)
    await retire(stopped, RetentionKey("publication", str(publication.id)), early=True)
    with pytest.raises(SkillContentError) as error:
        await retire(stopped, head, early=True)
    assert error.value.code == "HISTORY_REFERENCED"
    with pytest.raises(SkillContentError) as error:
        await retire(stopped, head, directory, early=True)
    assert error.value.code == "STATE_PROTECTED"


async def test_checkpoint_retirement_checks_each_retained_dependency(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    保留历史、目录成员与 backing 依赖阻断退役；整组退役后父审计关系仍可保留。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    selected, head_id, _ = await historical_selection(stopped, tmp_path)
    async with stopped.database() as session:
        index = await SkillRetentionRepository(session).load(stopped.owner)
        head = next(row for row in index.checkpoints if row.id == head_id)
        snapshot = next(row for row in index.snapshots if row.id == stopped.snapshot)
        item = next(row for row in index.snapshot_items if row.snapshot_id == snapshot.id)
        assert head.backing_directory_id is not None
        pairs = [
            (
                RetentionKey("checkpoint", str(head_id)),
                RetentionKey("checkpoint", str(head.backing_directory_id)),
            ),
            (
                RetentionKey("checkpoint", str(head.backing_directory_id)),
                RetentionKey("checkpoint", str(head_id)),
            ),
            (
                RetentionKey("snapshot", str(snapshot.id)),
                RetentionKey("checkpoint", str(item.checkpoint_id)),
            ),
            (
                RetentionKey("snapshot", str(snapshot.id)),
                RetentionKey("checkpoint", str(snapshot.starting_checkpoint_id)),
            ),
        ]
        for finalization in index.finalizations:
            pairs.append(
                (
                    RetentionKey("finalization", str(finalization.id)),
                    RetentionKey("checkpoint", str(finalization.checkpoint_id)),
                )
            )
        for publication in index.publications:
            pairs.append(
                (
                    RetentionKey("publication", str(publication.id)),
                    RetentionKey("checkpoint", str(publication.result_checkpoint_id)),
                )
            )
    for consumer, dependency in pairs:
        assert consumer in selected and dependency in selected
        # 先退役其他比较，避免父比较依赖掩盖所测 checkpoint 的直接消费者。
        async with stopped.database() as session, session.begin():
            service = SkillHistoryRetirementService(session, SkillStoragePolicy())
            comparisons = tuple(
                key for key in selected if key.kind != "checkpoint" and key != consumer
            )
            if consumer.kind == "snapshot":
                comparisons = tuple(
                    key for key in comparisons if key.kind in {"publication", "finalization"}
                )
            elif consumer.kind == "finalization":
                comparisons = tuple(key for key in comparisons if key.kind == "publication")
            elif consumer.kind == "publication":
                comparisons = ()
            if comparisons:
                await service.retire(
                    stopped.owner, stopped.account, comparisons, all_unreferenced=True
                )
            with pytest.raises(SkillContentError) as error:
                await service.retire(
                    stopped.owner, stopped.account, (dependency,), all_unreferenced=True
                )
            assert error.value.code == "HISTORY_REFERENCED"
            await session.rollback()
    assert await retire(stopped, *selected, early=True) == selected


async def test_checkpoint_retirement_rolls_back_expired_markers_and_invalid_batches(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    错误账户、混合无效身份和外层回滚均不能留下退役内容或分支过期标记。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    selected, head_id, _ = await historical_selection(stopped, tmp_path)
    async with stopped.database.begin() as session:
        service = SkillHistoryRetirementService(session, SkillStoragePolicy())
        for account, keys in (
            (uuid4(), selected),
            (stopped.account, (*selected, RetentionKey("checkpoint", str(uuid4())))),
        ):
            with pytest.raises(SkillContentError) as error:
                await service.retire(stopped.owner, account, keys, all_unreferenced=True)
            assert error.value.code == "HISTORY_NOT_FOUND"
    with pytest.raises(RuntimeError, match="rollback"):
        async with stopped.database.begin() as session:
            await SkillHistoryRetirementService(session, SkillStoragePolicy()).retire(
                stopped.owner, stopped.account, selected, all_unreferenced=True
            )
            branch = await session.get(AccountSkillState, stopped.state)
            assert branch is not None and branch.expired
            raise RuntimeError("rollback")
    async with stopped.database() as session:
        head = await session.get(SkillCheckpoint, head_id)
        branch = await session.get(AccountSkillState, stopped.state)
        assert head is not None and head.retained and head.tree_digest == head.content_digest
        assert branch is not None and not branch.expired and branch.head_checkpoint_id == head_id
        snapshots = await session.scalars(
            select(SessionSkillSnapshot).where(SessionSkillSnapshot.user_id == stopped.owner)
        )
        assert all(row.content_retired_at is None for row in snapshots)


async def test_retired_checkpoint_cannot_become_an_exact_active_input(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    迟到写入即使捕获错误后提交外层，也不能把当前目录重新指向已退役内容。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    selected, head_id, _ = await historical_selection(stopped, tmp_path)
    async with stopped.database() as session:
        head = await session.get(SkillCheckpoint, head_id)
        assert head is not None and head.backing_directory_id is not None
        old_directory = head.backing_directory_id
    await retire(stopped, *selected, early=True)
    current = (await command(stopped, tmp_path)).expected
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            async with retention_mutation(session, stopped.owner):
                index = await SkillRetentionRepository(session).load(stopped.owner)
                directory = next(
                    row for row in index.directories if row.account_id == stopped.account
                )
                directory.head_checkpoint_id = old_directory
        assert error.value.code == "STATE_EXPIRED"
    assert (await command(stopped, tmp_path)).expected == current


async def test_checkpoint_retirement_keeps_migration_baseline_and_full_comparison(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    精确增量基线先受硬保护；失效后完整比较仍要求同时退役，不能只清空其历史 checkpoint。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"learned"})
    )
    source, target = await versions(stopped, tmp_path)
    result = await migrate(
        stopped, tmp_path, await migration_request(stopped, tmp_path, source, target)
    )
    assert result.operation_id is not None
    migration_key = RetentionKey("migration", str(result.operation_id))
    async with stopped.database() as session:
        index = await SkillRetentionRepository(session).load(stopped.owner)
        migration = next(row for row in index.migrations if row.id == result.operation_id)
        assert migration.source_checkpoint_id is not None
        baseline = RetentionKey("checkpoint", str(migration.source_checkpoint_id))
    with pytest.raises(SkillContentError) as error:
        await retire(stopped, baseline, migration_key, early=True)
    assert error.value.code == "STATE_PROTECTED"
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    async with stopped.database() as session:
        inspector = await SkillRetentionInspector(session).inspect(stopped.owner)
        index = await SkillRetentionRepository(session).load(stopped.owner)
        selected = tuple(
            sorted(
                key
                for key in history_records(index)
                if key.kind
                in {"checkpoint", "snapshot", "finalization", "publication", "migration"}
                and not inspector.reasons(key.kind, key.identity)
            )
        )
    assert baseline in selected and migration_key in selected
    with pytest.raises(SkillContentError) as error:
        await retire(stopped, *(key for key in selected if key != migration_key), early=True)
    assert error.value.code == "HISTORY_REFERENCED" and "migration" in str(error.value)
    assert await retire(stopped, *selected, early=True) == selected


async def test_checkpoint_retirement_preserves_staged_local_original_context(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    未激活候选虽不是活动根，其保留初始版本仍承诺来源目录，拒绝退役后可以原身份重试。

    :param prepared (RuntimeHarness): 尚未运行的账户
    :param tmp_path (Path): 内容卷
    """
    from test_skill_local import register, source

    checkpoint = await source(prepared, tmp_path, linked=True)
    candidate = await register(prepared, tmp_path, checkpoint)
    async with prepared.database() as session:
        protected = await SkillRetentionInspector(session).inspect(prepared.owner)
        assert not protected.reasons("checkpoint", checkpoint.id)
    with pytest.raises(SkillContentError) as error:
        await retire(prepared, RetentionKey("checkpoint", str(checkpoint.id)), early=True)
    assert error.value.code == "HISTORY_REFERENCED" and "local_revision" in str(error.value)
    assert (await register(prepared, tmp_path, checkpoint)).id == candidate.id
    async with prepared.database() as session:
        original = await session.get(SkillCheckpoint, checkpoint.id)
        assert original is not None and original.retained
