"""
验证历史内容退役的完整保护、原子回滚、审计保留和独立增量基线。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_migration import migrate, request, versions
from test_skill_resolution_service import choose, pending, upload_tree
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command, execute

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_resolution import SkillResolutionChoice as ChoiceRow
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_storage import SkillStoredTree
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.conflicts import SkillConflictService
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration import SkillMigrationService
from agent_remote_server.services.skills.retention import SkillRetentionInspector
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.services.skills.retention.trees import StoredTreeReference
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def retire(
    state: RuntimeHarness, *keys: RetentionKey, early: bool = False
) -> tuple[RetentionKey, ...]:
    """
    每次使用独立真实事务，调用方提供精确账户与历史身份。

    :param state (RuntimeHarness): 已授权测试账户
    :param keys (RetentionKey): 本次选定历史
    :param early (bool): 是否显式提前结束历史等待
    :return tuple[RetentionKey, ...]: 实际退役身份
    """
    async with state.database.begin() as session:
        return await SkillHistoryRetirementService(session, SkillStoragePolicy()).retire(
            state.owner, state.account, keys, all_unreferenced=early
        )


async def test_expired_publication_preserves_choices_but_cannot_read_shared_bytes(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    原选择、摘要与回执保留，生成外键解除；即使同摘要树仍存在也不能读取已退役比较。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    conflict = await pending(stopped, tmp_path)
    digest = await upload_tree(stopped, tmp_path, {"content": b"retired custom content"})
    choice = SkillResolutionChoice(path="learning/one", file_tree_digest=digest)
    key = str(uuid4())
    saved = await choose(stopped, tmp_path, conflict, choice, key=key)
    identity = RetentionKey("publication", str(conflict.id))
    with pytest.raises(SkillContentError) as error:
        await retire(stopped, identity, early=True)
    assert error.value.code == "STATE_PROTECTED"
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    before = (await command(stopped, tmp_path)).expected
    with pytest.raises(SkillContentError) as error:
        await retire(stopped, identity)
    assert error.value.code == "HISTORY_NOT_EXPIRED"
    async with stopped.database.begin() as session:
        publication = await session.get(SkillPublication, conflict.id)
        assert publication is not None
        original = (publication.current_tree_digest, publication.conflicts_json)
        publication.retention_released_at = datetime.now(UTC) - timedelta(days=31)
        row = await session.scalar(select(ChoiceRow).where(ChoiceRow.publication_id == conflict.id))
        assert row is not None
        expected_reference = StoredTreeReference(
            "skill_resolution_choices",
            (str(conflict.id), row.selector_key),
            "retained_tree_digest",
            stopped.account,
        )
        inventory = await SkillRetentionInspector(session).trees(
            stopped.owner, SkillStoragePolicy()
        )
        custom = next(tree for tree in inventory if tree.key == RetentionKey("state_tree", digest))
        assert custom.references == (expected_reference,)
    assert await retire(stopped, identity) == (identity,)
    assert await retire(stopped, identity) == ()
    async with stopped.database() as session:
        publication = await session.get(SkillPublication, conflict.id)
        assert publication is not None and publication.content_retired_at is not None
        assert (publication.current_tree_digest, publication.conflicts_json) == original
        assert publication.retained_current_tree_digest is None
        row = await session.scalar(select(ChoiceRow).where(ChoiceRow.publication_id == conflict.id))
        assert row is not None and row.tree_digest == digest and row.retained_tree_digest is None
        assert row.content_retired_at == publication.content_retired_at
        inventory = await SkillRetentionInspector(session).trees(
            stopped.owner, SkillStoragePolicy()
        )
        custom = next(tree for tree in inventory if tree.key == RetentionKey("state_tree", digest))
        assert not custom.references
        assert await session.get(SkillStoredTree, (stopped.owner, "state", digest)) is not None
        service = SkillConflictService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        info = await service.info(stopped.owner, conflict.id)
        assert all(side.tree_digest is None for side in (info.base, info.current, info.incoming))
        assert info.choices
        with pytest.raises(SkillContentError) as error:
            await service.diff(stopped.owner, conflict.id)
        assert error.value.code == "STATE_EXPIRED"
    assert await choose(stopped, tmp_path, conflict, choice, key=key) == saved
    with pytest.raises(SkillContentError) as error:
        await choose(stopped, tmp_path, conflict, choice)
    assert error.value.code == "STATE_EXPIRED"
    assert (await command(stopped, tmp_path)).expected == before


async def test_retirement_respects_parent_histories_and_rollback(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    snapshot 不能先于完整收尾和发布退役，整组通过后仍须服从外层回滚。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    finalization_id = await ingest(stopped, tmp_path, {})
    publication = await publish(stopped, tmp_path, finalization_id)
    keys = (
        RetentionKey("snapshot", str(stopped.snapshot)),
        RetentionKey("finalization", str(finalization_id)),
        RetentionKey("publication", str(publication.id)),
    )
    for selected in ((keys[0],), (keys[1],)):
        with pytest.raises(SkillContentError) as error:
            await retire(stopped, *selected, early=True)
        assert error.value.code == "HISTORY_REFERENCED"
    with pytest.raises(RuntimeError, match="outer rollback"):
        async with stopped.database.begin() as session:
            await SkillHistoryRetirementService(session, SkillStoragePolicy()).retire(
                stopped.owner, stopped.account, keys, all_unreferenced=True
            )
            raise RuntimeError("outer rollback")
    async with stopped.database() as session:
        snapshot = await session.get(SessionSkillSnapshot, stopped.snapshot)
        assert snapshot is not None and snapshot.content_retired_at is None
        assert snapshot.retained_tree_digest == snapshot.tree_digest
    assert await retire(stopped, *keys, early=True) == tuple(sorted(keys))
    async with stopped.database() as session:
        snapshot = await session.get(SessionSkillSnapshot, stopped.snapshot)
        finalization = await session.get(SkillFinalization, finalization_id)
        assert snapshot is not None and finalization is not None
        assert snapshot.content_retired_at == finalization.content_retired_at
        assert snapshot.retained_tree_digest is None and finalization.retained_tree_digest is None
        assert snapshot.tree_digest and finalization.tree_digest == finalization.incoming_digest
        assert snapshot.session_reference_id == stopped.session


async def test_selection_is_account_bound_and_validated_before_any_retirement(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    不存在或不同账户身份使整份选择失败，不能先退役其中合法的一项。

    :param stopped (RuntimeHarness): 所有者账户
    :param tmp_path (Path): 内容卷
    """
    publication = await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    key = RetentionKey("publication", str(publication.id))
    async with stopped.database.begin() as session:
        service = SkillHistoryRetirementService(session, SkillStoragePolicy())
        for account, selected in (
            (uuid4(), (key,)),
            (stopped.account, (key, RetentionKey("publication", str(uuid4())))),
        ):
            with pytest.raises(SkillContentError) as error:
                await service.retire(stopped.owner, account, selected, all_unreferenced=True)
            assert error.value.code == "HISTORY_NOT_FOUND"
    async with stopped.database() as session:
        row = await session.get(SkillPublication, publication.id)
        assert row is not None and row.content_retired_at is None


async def test_retired_success_keeps_exact_incremental_baseline(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    退役完整成功比较不丢掉 last-migrated，后来源增量仍按原 checkpoint 合并。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"one"}))
    late = await new_session(stopped, tmp_path)
    source, target = await versions(stopped, tmp_path)
    first = await migrate(stopped, tmp_path, await request(stopped, tmp_path, source, target))
    assert first.operation_id is not None
    identity = RetentionKey("migration", str(first.operation_id))
    async with stopped.database() as session:
        inventory = await SkillRetentionInspector(session).trees(
            stopped.owner, SkillStoragePolicy()
        )
        migration_references = {
            ref.column
            for tree in inventory
            for ref in tree.references
            if ref.table == "skill_branch_preparations"
            and ref.identity == (str(first.operation_id),)
            and ref.account_id == stopped.account
        }
        assert migration_references == {
            "retained_base_digest",
            "retained_current_digest",
            "retained_incoming_digest",
        }
    assert await retire(stopped, identity, early=True) == (identity,)
    async with stopped.database() as session:
        row = await session.get(SkillBranchPreparation, first.operation_id)
        assert row is not None and row.content_retired_at is not None
        assert row.retained_base_digest is None and row.retained_current_digest is None
        assert row.retained_incoming_digest is None
        inventory = await SkillRetentionInspector(session).trees(
            stopped.owner, SkillStoragePolicy()
        )
        assert not any(
            ref.table == "skill_branch_preparations" and ref.identity == (str(first.operation_id),)
            for tree in inventory
            for ref in tree.references
        )
        assert row.response_json and row.base_digest and row.current_digest and row.incoming_digest
        view = await SkillRetentionInspector(session).inspect(stopped.owner)
        assert view.reasons("migration_baseline", first.operation_id)
        assert row.source_checkpoint_id is not None
        assert view.reasons("checkpoint", row.source_checkpoint_id)
        receipt = await SkillMigrationService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).receipt_by_id(stopped.owner, first.operation_id)
        assert receipt.result == first
    await publish(late, tmp_path, await ingest(late, tmp_path, {"learning/new": b"two"}))
    second = await migrate(stopped, tmp_path, await request(stopped, tmp_path, source, target))
    assert second.status == "ready" and second.operation_id != first.operation_id
    assert second.before.last_migration_id == first.operation_id
