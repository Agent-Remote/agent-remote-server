"""
验证真实数据库及完整内容卷恢复后仍可继续保存的冲突计划。
"""

import os
from dataclasses import replace
from pathlib import Path

import pytest
from skill_publication_support import directory_tree
from skill_restore_node import verify_new_node
from skill_restore_seed import seed_restore
from skill_restore_support import (
    RestoreEnvironment,
    content_fingerprint,
    database_fingerprint,
)
from skill_restore_support import (
    restore_environment as restore_environment,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_skill_library import LibraryHarness
from test_skill_resolution_service import choose

from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_storage import SkillStoredTree
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def test_database_and_content_restore_preserves_references_and_pending_plan(
    restore_environment: RestoreEnvironment,
) -> None:
    """
    销毁源数据库并移走源内容后，只凭正式归档恢复全部行、内容和冲突选择。

    :param restore_environment (RestoreEnvironment): 本测试拥有的一次性数据库与目录
    """
    environment = restore_environment
    source, restored = environment.root / "source", environment.root / "restored"
    engine = create_async_engine(environment.url("restore_source"))
    database = async_sessionmaker(engine, expire_on_commit=False)
    try:
        seed = await seed_restore(database, source)
        rows = await database_fingerprint(database)
        files = content_fingerprint(source)
        effective = await LibraryHarness(database, source, seed.state.owner).info(
            account=seed.state.account
        )
        assert effective.effective is not None and effective.effective.included
        assert (
            effective.effective.enabled_source == effective.effective.revision_source == "account"
        )
        assert rows["skill_revisions"][0] >= 3
        tree = await directory_tree(seed.state, source)
        for table in (
            "skill_revisions",
            "skill_tool_overrides",
            "skill_account_overrides",
            "account_skill_states",
            "skill_checkpoints",
            "skill_directory_members",
            "session_skill_snapshots",
            "skill_publications",
            "skill_resolution_plans",
            "skill_resolution_choices",
            "skill_resolution_operations",
            "skill_operations",
            "skill_finalizations",
            "session_skill_snapshot_items",
            "account_local_skills",
            "account_local_skill_revisions",
            "skill_deployment_targets",
            "skill_deployment_entries",
            "skill_deployment_attempts",
        ):
            assert rows[table][0] > 0, table
    finally:
        await engine.dispose()
    environment.restore(source, restored)
    assert not source.exists()
    engine = create_async_engine(environment.url("restore_target"))
    database = async_sessionmaker(engine, expire_on_commit=False)
    state = replace(seed.state, database=database)
    try:
        assert await database_fingerprint(database) == rows
        assert content_fingerprint(restored) == files
        assert (
            await LibraryHarness(database, restored, state.owner).info(account=state.account)
            == effective
        )
        assert await directory_tree(state, restored) == tree
        store = PrivateObjectStore(restored / "objects")
        async with database() as session:
            for saved in await session.scalars(select(SkillStoredTree)):
                await store.verify_manifest(
                    saved.user_id, SkillTreeManifest.model_validate(saved.manifest_json)
                )
            detached = list(
                await session.scalars(
                    select(SkillPublication).where(SkillPublication.status == "detached")
                )
            )
            assert len(detached) == 1 and detached[0].result_checkpoint_id is None
            publication = await session.get(SkillPublication, seed.publication_id)
            assert publication is not None and publication.status == "conflicted"
            assert publication.current_tree_digest is not None
            content = SkillContentService(session, store, SkillStoragePolicy())
            with pytest.raises(SkillContentError) as denied:
                await content.read_tree(seed.other_owner, "state", publication.current_tree_digest)
            assert denied.value.code == "CONTENT_NOT_FOUND"
        first = await choose(
            state,
            restored,
            publication,
            SkillResolutionChoice(path="learning/one", use="current"),
            key=seed.choice_key,
        )
        assert first.status == "pending" and first.plan_revision == 1
        result = await choose(
            state,
            restored,
            publication,
            SkillResolutionChoice(path="learning/two", use="incoming"),
            revision=1,
        )
        assert result.status == "published" and result.plan_revision == 2
        current = await directory_tree(state, restored)
        entries = {entry.path: entry for entry in current.entries}
        assert "learning/removed.txt" not in entries and "learning/recovered.txt" not in entries
        assert {
            "notes/state.bin",
            "root-helper.txt",
            "learning/one",
            "learning/two",
        } <= entries.keys()
        node_repo = Path(
            os.environ.get(
                "AGENT_REMOTE_TEST_NODE_REPO",
                str(Path(__file__).resolve().parents[2] / "agent-remote-node"),
            )
        )
        await verify_new_node(state, restored, node_repo)
    finally:
        await engine.dispose()
