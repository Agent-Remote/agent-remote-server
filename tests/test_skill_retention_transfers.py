"""
验证接管和人工迁移内容在完整事务生命周期中的保护边界。
"""

import io
from pathlib import Path
from uuid import uuid4

from skill_runtime_support import RuntimeHarness
from skill_takeover_support import TakeoverHarness, tree
from skill_takeover_support import takeover as takeover
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import library as library
from test_skill_migration_conflicts import pending
from test_skill_retention import inspect
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command, execute
from test_skill_storage import file_entry

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_resolution import SkillResolutionUploadRequest
from agent_remote_server.services.skills.migration_resolution_content import (
    SkillMigrationResolutionContentService,
)
from agent_remote_server.services.skills.retention import SkillRetentionInspector
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def test_pending_takeover_and_committed_initial_directory_have_distinct_roots(
    takeover: TakeoverHarness,
) -> None:
    """
    未完成接管保护其身份和上传，提交后由目录和本地原始状态接替保护。

    :param takeover (TakeoverHarness): 未接管账户
    """
    files = {"notes/SKILL.md": b"---\nname: notes\ndescription: Notes\n---\n", "root": b"context"}
    manifest = tree(files)
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    receipt = await takeover.begin(receipt, takeover.capture(receipt, manifest))
    async with takeover.library.database() as session:
        result = await SkillRetentionInspector(session).inspect(takeover.library.owner)
        assert result.reasons("takeover", receipt.id) == {"pending_takeover"}
        assert receipt.upload_id is not None
        assert "pending_takeover" in result.reasons("upload", receipt.upload_id)
        for entry in manifest.entries:
            if entry.kind == "file":
                assert result.reasons("blob", entry.sha256)
    await takeover.transfer(receipt, files)
    receipt = await takeover.complete(receipt)
    async with takeover.library.database() as session:
        result = await SkillRetentionInspector(session).inspect(takeover.library.owner)
        assert not result.reasons("takeover", receipt.id)
        assert receipt.checkpoint_id is not None
        assert {"current_directory", "local_original"} <= result.reasons(
            "checkpoint", receipt.checkpoint_id
        )
        assert len(result.directory_members) == 1


async def test_migration_authorized_custom_tree_is_protected_before_a_choice_is_saved(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    已完成授权的人工内容属于未解决冲突，不能在计划提交前作为孤立树回收。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有内容卷
    """
    original = await pending(stopped, tmp_path)
    assert original.operation_id is not None
    entry = file_entry(b"manual merged result")
    assert entry.sha256 is not None
    async with stopped.database.begin() as session:
        service = SkillMigrationResolutionContentService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        upload = await service.begin(
            stopped.owner,
            original.operation_id,
            SkillResolutionUploadRequest(
                idempotency_key=str(uuid4()), manifest=SkillTreeManifest(entries=(entry,))
            ),
        )
        await service.put_file(
            stopped.owner,
            original.operation_id,
            upload.id,
            entry.sha256,
            io.BytesIO(b"manual merged result"),
        )
        completed = await service.complete(stopped.owner, original.operation_id, upload.id)
        digest = completed.digest
    result = await inspect(stopped)
    assert result.reasons("state_tree", digest) == {"migration_conflict"}
    assert result.reasons("blob", entry.sha256) == {"migration_conflict"}
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    assert not (await inspect(stopped)).reasons("state_tree", digest)
