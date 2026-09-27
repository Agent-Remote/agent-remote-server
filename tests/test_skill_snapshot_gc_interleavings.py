"""
验证精确会话预约与内容回收的两个提交次序，以及已删除标记的原子拒绝。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from skill_publication_support import baseline, new_session
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_gc import preview
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared
from test_skill_snapshots import reserve

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, AccountSkillState
from agent_remote_server.models.skill_storage import SkillContentObject, SkillContentUpload
from agent_remote_server.schemas.skill_library import SkillRuleRequest
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.gc import SkillContentReclamationService
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@pytest.mark.parametrize("snapshot_first", [False, True])
async def test_filtered_snapshot_and_gc_revalidate_both_commit_orders(
    stopped: RuntimeHarness, tmp_path: Path, snapshot_first: bool
) -> None:
    """
    新过滤目录命中同摘要的无引用树；预约先提交阻断旧回收计划，回收先提交允许安全重建引用。

    :param stopped (RuntimeHarness): 已实际使用的账户
    :param tmp_path (Path): 私有内容卷
    :param snapshot_first (bool): 精确预约是否先于回收提交
    """
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.add(await library.candidate(name="second"), await library.candidate(name="third"))
    both = await new_session(stopped, tmp_path)
    complete = await baseline(both, tmp_path)
    filtered = SkillTreeManifest(
        entries=tuple(
            entry for entry in complete.entries if entry.path.split("/", 1)[0] != "learning"
        )
    )
    assert filtered.entries and filtered != complete
    async with stopped.database.begin() as session:
        content = content_service(session, tmp_path)
        upload = await content.begin(stopped.owner, str(uuid4()), filtered, "state")
        tree = await content.complete(stopped.owner, upload.id)
        key = RetentionKey("state_tree", tree.digest)
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="learning",
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    plan = await preview(stopped.database, stopped.owner, key)
    assert plan.ready and plan.pending_file_bytes == 0
    original = await new_session(stopped, tmp_path) if snapshot_first else None
    async with stopped.database.begin() as session:
        gc = SkillContentReclamationService(session, SkillStoragePolicy())
        if snapshot_first:
            with pytest.raises(SkillContentError) as error:
                await gc.apply(stopped.owner, plan)
            assert error.value.code == "HEAD_CHANGED"
        else:
            result = await gc.apply(stopped.owner, plan)
            assert result.trees == (key,) and not result.deletion_ids
    actual = original or await new_session(stopped, tmp_path)
    snapshot = await reserve(actual, tmp_path)
    assert snapshot.tree_digest == key.identity and snapshot.id == actual.snapshot
    assert await baseline(actual, tmp_path) == filtered
    protected = await preview(stopped.database, stopped.owner, key)
    assert not protected.ready and protected.blockers
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillContentReclamationService(session, SkillStoragePolicy()).apply(
                stopped.owner, protected
            )
        assert error.value.code == "CONTENT_REFERENCED"
        assert not list(
            await session.scalars(
                select(SkillContentDeletion).where(SkillContentDeletion.user_id == stopped.owner)
            )
        )


async def test_preexisting_cross_category_deletion_marker_cannot_gain_snapshot_references(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    注入已提交的共享文件删除标记后，真实预约整体拒绝且不留下快照、分支 head 或新上传。

    :param prepared (RuntimeHarness): 尚未预约的原始会话与任务
    :param tmp_path (Path): 私有内容卷
    """
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    info = await library.info()
    revision = next(row for row in info.revisions if row.id == info.default_revision_id)
    async with prepared.database.begin() as session:
        content = content_service(session, tmp_path)
        manifest = await content.read_tree(prepared.owner, "package", revision.content_digest)
        upload = await content.begin(prepared.owner, str(uuid4()), manifest, "state")
        await content.complete(prepared.owner, upload.id)
        marker = await session.get(
            SkillContentObject, (prepared.owner, "state", manifest.entries[0].sha256)
        )
        assert marker is not None
        marker.status = "deleting"
        count = await session.scalar(
            select(func.count())
            .select_from(SkillContentUpload)
            .where(SkillContentUpload.user_id == prepared.owner)
        )
    with pytest.raises(SkillContentError) as error:
        await reserve(prepared, tmp_path)
    assert error.value.code == "CONTENT_UNAVAILABLE"
    async with prepared.database() as session:
        assert not list(
            await session.scalars(
                select(SessionSkillSnapshot).where(SessionSkillSnapshot.user_id == prepared.owner)
            )
        )
        branch = await session.get(AccountSkillState, prepared.state)
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert branch is not None and branch.head_checkpoint_id is None
        assert directory is not None and directory.head_checkpoint_id == prepared.directory
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillContentUpload)
                .where(SkillContentUpload.user_id == prepared.owner)
            )
            == count
        )
