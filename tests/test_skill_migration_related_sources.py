"""
验证关联迁移沿确切目录证明来源身份和历史纪元，不以同名字节冒充稳定来源。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration import migrate, request, versions
from test_skill_migration_resolution_plan import calculate
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import SkillCheckpoint, SkillDirectoryMember
from agent_remote_server.schemas.skill_library import SkillRemoveRequest
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_resolution import (
    SkillResolutionChoice,
    SkillResolutionUploadRequest,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_resolution_content import (
    SkillMigrationResolutionContentService,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def linked_sources(state: RuntimeHarness, root: Path, change: str) -> SkillMigrationView:
    """
    两个真实稳定来源先形成链接，再在原来源树之外改变另一成员。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 内容卷
    :param change (str): 不变、同纪元推进、重置或同字节重装
    :return SkillMigrationView: 完整保存的真实迁移冲突
    """
    library = LibraryHarness(state.database, root, state.owner)
    await library.add(await library.candidate(name="notes"))
    active = await new_session(state, root)
    await publish(
        active, root, await ingest(active, root, {}, links={"learning/link": "../notes/SKILL.md"})
    )
    if change == "head":
        newer = await new_session(state, root)
        await publish(newer, root, await ingest(newer, root, {"notes/memory": b"new context"}))
    elif change == "reset":
        await state_execute(state, root, await command(state, root, skill="notes"))
    elif change == "reinstall":
        await library.execute(
            SkillRemoveRequest(
                skill="notes",
                idempotency_key=str(uuid4()),
                expected_generation=await library.generation(),
            )
        )
        await library.add(await library.candidate(name="notes"))
        await state_execute(state, root, await command(state, root, skill="notes"))
    source_revision, target_revision = await versions(state, root)
    saved = await migrate(state, root, await request(state, root, source_revision, target_revision))
    assert saved.status == "conflicted" and saved.operation_id is not None
    return saved


@pytest.mark.parametrize("change", ["none", "head"])
async def test_explicit_linked_incoming_accepts_matching_identity_and_epoch(
    stopped: RuntimeHarness, tmp_path: Path, change: str
) -> None:
    """
    同身份同纪元的历史关联内容可明确整体选择，不要求关联 checkpoint 恰好仍为 head。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param change (str): 是否有较新的同纪元关联内容
    """
    saved = await linked_sources(stopped, tmp_path, change)
    assert saved.operation_id is not None
    result = await calculate(
        stopped, tmp_path, saved.operation_id, [SkillResolutionChoice(use="incoming")]
    )
    assert result.result.merged is not None and result.unit == ("learning", "notes")
    assert result.other_changed_roots == (("notes",) if change == "head" else ())
    assert all(entry.path != "notes/memory" for entry in result.result.merged.entries)


@pytest.mark.parametrize(
    "change,code", [("reset", "STATE_EPOCH_CHANGED"), ("reinstall", "SKILL_SOURCE_CONFLICT")]
)
async def test_equal_bytes_do_not_authorize_old_epoch_or_replaced_identity(
    stopped: RuntimeHarness, tmp_path: Path, change: str, code: str
) -> None:
    """
    关联成员字节完全相等也不能绕过 reset 或同名重装；保留当前身份仍可计算。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param change (str): 关联身份失效方式
    :param code (str): 预期明确错误
    """
    saved = await linked_sources(stopped, tmp_path, change)
    assert saved.operation_id is not None
    current = await calculate(
        stopped, tmp_path, saved.operation_id, [SkillResolutionChoice(use="current")]
    )
    before = [entry for entry in current.inputs.directory.entries if entry.path.startswith("notes")]
    incoming = [
        entry for entry in current.inputs.incoming.entries if entry.path.startswith("notes")
    ]
    assert before == incoming
    with pytest.raises(SkillContentError) as error:
        await calculate(
            stopped, tmp_path, saved.operation_id, [SkillResolutionChoice(use="incoming")]
        )
    assert error.value.code == code
    assert current.result.merged is not None and current.other_changed_roots == ()


@pytest.mark.parametrize("missing", ["backing", "source-epoch", "related-epoch"])
async def test_unknown_provenance_is_not_recovered_from_equal_tree_or_current_state(
    stopped: RuntimeHarness, tmp_path: Path, missing: str
) -> None:
    """
    模拟升级前的未知证据，即使完整内容及实时分支仍在，也不猜测历史来源。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param missing (str): 既有历史缺失字段
    """
    saved = await linked_sources(stopped, tmp_path, "none")
    assert saved.operation_id is not None
    current = await calculate(
        stopped, tmp_path, saved.operation_id, [SkillResolutionChoice(use="current")]
    )
    async with stopped.database.begin() as session:
        migration = await session.get(SkillBranchPreparation, saved.operation_id)
        assert migration is not None and migration.source_checkpoint_id is not None
        source = await session.get(SkillCheckpoint, migration.source_checkpoint_id)
        assert source is not None
        if missing == "backing":
            source.backing_directory_id = None
        elif missing == "source-epoch":
            source.state_epoch = None
        else:
            member = await session.get(SkillDirectoryMember, (source.backing_directory_id, "notes"))
            assert member is not None
            related = await session.get(SkillCheckpoint, member.checkpoint_id)
            assert related is not None
            related.state_epoch = None
    with pytest.raises(SkillContentError) as error:
        await calculate(
            stopped, tmp_path, saved.operation_id, [SkillResolutionChoice(use="incoming")]
        )
    assert error.value.code == "STATE_PROVENANCE_UNAVAILABLE"
    assert current.result.merged is not None


async def test_custom_linked_result_explicitly_targets_saved_current_identities(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    人工整目录结果使用保存目录的稳定目标，不把历史输入侧的同名旧身份一起导入。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    saved = await linked_sources(stopped, tmp_path, "reinstall")
    assert saved.operation_id is not None
    current = await calculate(
        stopped, tmp_path, saved.operation_id, [SkillResolutionChoice(use="current")]
    )
    async with stopped.database.begin() as session:
        service = SkillMigrationResolutionContentService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        upload = await service.begin(
            stopped.owner,
            saved.operation_id,
            SkillResolutionUploadRequest(
                idempotency_key=str(uuid4()), manifest=current.inputs.incoming
            ),
        )
        tree = await service.complete(stopped.owner, saved.operation_id, upload.id)
        digest = tree.digest
    resolved = await calculate(
        stopped, tmp_path, saved.operation_id, [SkillResolutionChoice(directory_tree_digest=digest)]
    )
    assert resolved.result.merged is not None and resolved.other_changed_roots == ()
