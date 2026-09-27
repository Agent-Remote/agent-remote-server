"""
验证初次和旧版准备的真实目录链接冲突，解决不能虚构向前迁移成功基线。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration_conflicts import counts
from test_skill_migration_resolution_drafts import edit_request, saved_plan
from test_skill_migration_resolution_service import resolve
from test_skill_preparation import pin, prepare, request, update_version
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, SkillCheckpoint
from agent_remote_server.schemas.skill_library import SkillRuleRequest
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.schemas.skill_preparation import SkillPreparationView
from agent_remote_server.schemas.skill_resolution import (
    SkillResolutionChoice,
    SkillResolutionUploadRequest,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_conflicts import SkillMigrationConflictService
from agent_remote_server.services.skills.migration_resolution_content import (
    SkillMigrationResolutionContentService,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def repair(
    state: RuntimeHarness, root: Path, original: SkillPreparationView, incoming: bool = False
) -> None:
    """
    使用保留原始字节构造完整人工修复，并验证预览、发布、重放和实时诊断。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 内容卷
    :param original (SkillPreparationView): 真实链接冲突响应
    :param incoming (bool): 是否明确保留有来源的完整旧侧
    """
    identity = original.operation_id
    assert identity is not None and original.status == "conflicted"
    assert original.conflicts[0].reason == "invalid_tree"
    async with state.database.begin() as session:
        service = SkillMigrationConflictService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        )
        info = await service.info(state.owner, identity)
        assert info.current.tree_digest is not None
        current = await service.queries.content.read_tree(
            state.owner, "state", info.current.tree_digest
        )
        manifest = SkillTreeManifest(
            entries=tuple(
                sorted(
                    (
                        *current.entries,
                        SkillTreeEntry(
                            path="aux", kind="symlink", mode=0o777, target="learning/SKILL.md"
                        ),
                    ),
                    key=lambda entry: entry.path,
                )
            )
        )
        upload_service = SkillMigrationResolutionContentService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        )
        upload = await upload_service.begin(
            state.owner,
            identity,
            SkillResolutionUploadRequest(idempotency_key=str(uuid4()), manifest=manifest),
        )
        custom = await upload_service.complete(state.owner, identity, upload.id)
    choice = (
        SkillResolutionChoice(use="incoming")
        if incoming
        else SkillResolutionChoice(directory_tree_digest=custom.digest)
    )
    payload = edit_request(choice)
    preview = await resolve(state, root, identity, payload.model_copy(update={"dry_run": True}))
    assert preview.candidate_complete and preview.migration_sequence is None
    result = await resolve(state, root, identity, payload)
    assert result.status == "published" and result.migration_sequence is None
    assert result.result_tree_digest == preview.result_tree_digest
    assert await resolve(state, root, identity, payload) == result
    async with state.database.begin() as session:
        row = await session.get(SkillBranchPreparation, identity)
        assert row is not None and row.status == "ready" and row.mode == original.mode
        assert row.migration_sequence is None and row.response_json == original.model_dump(
            mode="json"
        )
        info = await SkillMigrationConflictService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).info(state.owner, identity)
        assert info.original == original and not info.live.recomputation_reasons
        assert info.live.last_migration_id is None


async def initial_conflict(prepared: RuntimeHarness, tmp_path: Path) -> SkillPreparationView:
    """
    从合法保留目录建立真实初次准备冲突，不伪造迁移记录。

    :param prepared (RuntimeHarness): 未使用库分支的账户
    :param tmp_path (Path): 内容卷
    :return SkillPreparationView: 原始悬空链接冲突
    """
    raw = SkillTreeManifest(
        entries=(
            SkillTreeEntry(path="aux", kind="symlink", mode=0o777, target="learning/memory"),
            SkillTreeEntry(path="learning", kind="directory", mode=0o755),
            SkillTreeEntry(path="learning/memory", kind="directory", mode=0o755),
        )
    )
    async with prepared.database.begin() as session:
        content = content_service(session, tmp_path)
        upload = await content.begin(prepared.owner, str(uuid4()), raw, "state")
        tree = await content.complete(prepared.owner, upload.id)
        checkpoint = SkillCheckpoint(
            user_id=prepared.owner,
            account_id=prepared.account,
            scope="directory",
            content_digest=tree.digest,
            tree_digest=tree.digest,
            directory_epoch=1,
        )
        session.add(checkpoint)
        await session.flush()
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert directory is not None
        directory.head_checkpoint_id = checkpoint.id
    original = await prepare(prepared, tmp_path, await request(prepared, tmp_path))
    assert original.mode == "initial" and original.source_checkpoint_id is None
    return original


async def test_initial_link_conflict_repairs_without_source_or_sequence(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    初次目录链接修复不需要虚构来源或成功序号。

    :param prepared (RuntimeHarness): 未使用库分支的接管账户
    :param tmp_path (Path): 私有卷
    """
    await repair(prepared, tmp_path, await initial_conflict(prepared, tmp_path))


@pytest.mark.parametrize("incoming", [False, True])
async def test_older_link_conflict_preserves_non_migration_semantics(
    stopped: RuntimeHarness, tmp_path: Path, incoming: bool
) -> None:
    """
    使用新版后首次进入未用旧版会因反向链接冲突，明确解决仍不创建增量序号。

    :param stopped (RuntimeHarness): 已实际使用原始版本的账户
    :param tmp_path (Path): 私有卷
    :param incoming (bool): 是否显式导入保存的较新版内容
    """
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"learned"})
    )
    unused = await update_version(stopped, tmp_path, "unused")
    await update_version(stopped, tmp_path, "latest")
    await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    latest = await new_session(stopped, tmp_path)
    linked = await publish(
        latest, tmp_path, await ingest(latest, tmp_path, {}, links={"aux": "learning/memory"})
    )
    assert linked.status == "published"
    await pin(stopped, tmp_path, unused)
    original = await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    assert original.mode == "older" and original.source_checkpoint_id is not None
    await repair(stopped, tmp_path, original, incoming)


async def test_initial_incoming_cannot_claim_linked_named_source(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    无使用历史的原始包没有关联成员身份，整体 incoming 不能冒充其来源并删除成员。

    :param prepared (RuntimeHarness): 尚未使用学习技能的账户
    :param tmp_path (Path): 内容卷
    """
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="learning",
            idempotency_key=str(uuid4()),
            expected_generation=await library.generation(),
        )
    )
    await library.add(await library.candidate(name="notes"))
    active = await new_session(prepared, tmp_path)
    linked = await publish(
        active,
        tmp_path,
        await ingest(
            active,
            tmp_path,
            {"learning/memory": b"raw retained context"},
            links={"notes/link": "../learning/memory"},
        ),
    )
    assert linked.status == "published"
    await library.execute(
        SkillRuleRequest(
            command="enable",
            skill="learning",
            idempotency_key=str(uuid4()),
            expected_generation=await library.generation(),
        )
    )
    original = await prepare(prepared, tmp_path, await request(prepared, tmp_path))
    identity = original.operation_id
    assert original.mode == "initial" and original.status == "conflicted" and identity is not None
    before = await counts(prepared)
    payload = edit_request(SkillResolutionChoice(use="incoming"))
    for dry_run in (True, False):
        with pytest.raises(SkillContentError) as error:
            await resolve(
                prepared, tmp_path, identity, payload.model_copy(update={"dry_run": dry_run})
            )
        assert error.value.code == "STATE_PROVENANCE_UNAVAILABLE"
    assert await counts(prepared) == before
    assert await saved_plan(prepared, identity) == (0, [], 0)
