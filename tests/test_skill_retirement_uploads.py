"""
验证人工输入租约、原授权主键及退役后的内容读取与外键释放。
"""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_history_retirement import retire
from test_skill_migration_conflicts import pending
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command, execute
from test_skill_storage import file_entry

from agent_remote_server.models.skill_migration_resolution import SkillMigrationResolutionContent
from agent_remote_server.models.skill_storage import SkillStoredTree, SkillTreeObjectReference
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_resolution import SkillResolutionUploadRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_conflict_content import (
    SkillMigrationConflictContent,
)
from agent_remote_server.services.skills.migration_conflicts import SkillMigrationConflictService
from agent_remote_server.services.skills.migration_resolution_content import (
    SkillMigrationResolutionContentService,
)
from agent_remote_server.services.skills.retention import SkillRetentionInspector
from agent_remote_server.services.skills.retention.planner import SkillHistoryRetirementPlanner
from agent_remote_server.services.skills.retention.trees import StoredTreeReference
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def test_retirement_preserves_upload_receipt_and_custom_authorization_identity(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    活动上传阻止退役，完成后可退役授权内容而保留复合主键，原状态仍可读。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    """
    original = await pending(stopped, tmp_path)
    migration_id = original.operation_id
    assert migration_id is not None
    key = RetentionKey("migration", str(migration_id))
    data = b"unique retirement custom content"
    entry = file_entry(data, path="content")
    assert entry.sha256 is not None
    store, policy = PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
    async with stopped.database.begin() as session:
        upload = await SkillMigrationResolutionContentService(session, store, policy).begin(
            stopped.owner,
            migration_id,
            SkillResolutionUploadRequest(
                idempotency_key=str(uuid4()), manifest=SkillTreeManifest(entries=(entry,))
            ),
        )
        upload_id = upload.id
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    with pytest.raises(SkillContentError) as error:
        await retire(stopped, key, early=True)
    assert error.value.code == "STATE_PROTECTED"
    async with stopped.database() as session:
        planner = SkillHistoryRetirementPlanner(session, policy)
        planned = await planner.preview(
            stopped.owner, stopped.account, (key,), all_unreferenced=True
        )
        assert not planned.ready and "active_upload" in planned.entries[0].blockers
        repository = SkillRetentionRepository(session)
        identities = (*tuple(uuid4() for _ in range(501)), migration_id)
        assert await repository.active_migration_ids(
            stopped.owner, identities, datetime.now(UTC)
        ) == {migration_id}
        assert not await repository.active_migration_ids(uuid4(), identities, datetime.now(UTC))
        assert not await repository.active_migration_ids(
            stopped.owner, identities, datetime.now(UTC) + timedelta(days=7)
        )
    async with stopped.database.begin() as session:
        service = SkillMigrationResolutionContentService(session, store, policy)
        await service.put_file(stopped.owner, migration_id, upload_id, entry.sha256, BytesIO(data))
        tree = await service.complete(stopped.owner, migration_id, upload_id)
        digest = tree.digest
    expected_reference = StoredTreeReference(
        "skill_migration_resolution_content",
        (str(migration_id), digest),
        "retained_tree_digest",
        stopped.account,
    )
    async with stopped.database() as session:
        inventory = await SkillRetentionInspector(session).trees(stopped.owner, policy)
        custom = next(row for row in inventory if row.key == RetentionKey("state_tree", digest))
        assert custom.references == (expected_reference,)
    with pytest.raises(IntegrityError):
        async with stopped.database.begin() as session:
            await session.execute(
                delete(SkillTreeObjectReference).where(
                    SkillTreeObjectReference.user_id == stopped.owner,
                    SkillTreeObjectReference.category == "state",
                    SkillTreeObjectReference.tree_digest == digest,
                )
            )
            await session.execute(
                delete(SkillStoredTree).where(
                    SkillStoredTree.user_id == stopped.owner,
                    SkillStoredTree.category == "state",
                    SkillStoredTree.digest == digest,
                )
            )
    async with stopped.database() as session:
        planned = await SkillHistoryRetirementPlanner(session, policy).preview(
            stopped.owner, stopped.account, (key,), all_unreferenced=True
        )
        assert planned.ready
    assert await retire(stopped, key, early=True) == (key,)
    async with stopped.database.begin() as session:
        grant = await session.get(SkillMigrationResolutionContent, (migration_id, digest))
        assert grant is not None and grant.content_retired_at is not None
        assert grant.tree_digest == digest and grant.retained_tree_digest is None
        inventory = await SkillRetentionInspector(session).trees(stopped.owner, policy)
        custom = next(row for row in inventory if row.key == RetentionKey("state_tree", digest))
        assert not custom.references
        service = SkillMigrationResolutionContentService(session, store, policy)
        assert (await service.get(stopped.owner, migration_id, upload_id)).status == "committed"
        with pytest.raises(SkillContentError) as error:
            await service.complete(stopped.owner, migration_id, upload_id)
        assert error.value.code == "STATE_EXPIRED"
        conflicts = SkillMigrationConflictService(session, store, policy)
        info = await conflicts.info(stopped.owner, migration_id)
        assert info.original == original
        assert all(
            side.tree_digest is None
            for side in (info.base, info.current, info.incoming, info.directory)
        )
        with pytest.raises(SkillContentError) as error:
            await SkillMigrationConflictContent(conflicts).diff(stopped.owner, migration_id)
        assert error.value.code == "STATE_EXPIRED"
        await session.execute(
            delete(SkillTreeObjectReference).where(
                SkillTreeObjectReference.user_id == stopped.owner,
                SkillTreeObjectReference.category == "state",
                SkillTreeObjectReference.tree_digest == digest,
            )
        )
        await session.execute(
            delete(SkillStoredTree).where(
                SkillStoredTree.user_id == stopped.owner,
                SkillStoredTree.category == "state",
                SkillStoredTree.digest == digest,
            )
        )
    async with stopped.database() as session:
        assert (
            await session.get(SkillMigrationResolutionContent, (migration_id, digest)) is not None
        )
        assert await session.get(SkillStoredTree, (stopped.owner, "state", digest)) is None
