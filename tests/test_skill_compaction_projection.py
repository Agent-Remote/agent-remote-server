"""
以真实整理发布结果校验只读保护投影，防止旧历史引用和预计等待被虚假释放。
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_directory_compaction import apply, fingerprint, preview, shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_preparation import pin
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_state import AccountSkillState, SkillCheckpoint
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.services.skills.compaction import SkillDirectoryCompactionService
from agent_remote_server.services.skills.compaction.plan import CompactionPlan, CompactionResult
from agent_remote_server.services.skills.compaction.projection import (
    CompactionRetentionPreview,
    project_retention,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention import SkillRetentionInspector
from agent_remote_server.services.skills.retention.checkpoint_dependencies import (
    require_checkpoint_dependencies,
)
from agent_remote_server.skill_manager.retention.graph import RetentionKey, RetentionProtection
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def projected(
    state: RuntimeHarness, root: Path, plan: CompactionPlan
) -> CompactionRetentionPreview:
    """
    在会提交的事务中运行投影并刷新，以暴露任何错误修改或隐式持久化。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 私有内容卷
    :param plan (CompactionPlan): 精确预览计划
    :return CompactionRetentionPreview: 未持久化的保护假设
    """
    async with state.database.begin() as session:
        result = await SkillDirectoryCompactionService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).retention_preview(state.owner, plan)
        assert not session.dirty and not session.new and not session.deleted
        await session.flush()
        return result


def mapped_protection(
    predicted: CompactionRetentionPreview, result: CompactionResult
) -> RetentionProtection:
    """
    只按发布的原始身份映射虚拟 UUID，完整比较理由、对象、根及当前成员。

    :param predicted (CompactionRetentionPreview): 原始投影
    :param result (CompactionResult): 真实原子发布身份
    :return RetentionProtection: 使用持久化身份表达的预期完整图
    """
    actual = dict((*result.directory_replacements, *result.head_replacements))
    identities = {row.id: actual[row.original_id] for row in predicted.projection.checkpoints}

    def key(original: RetentionKey) -> RetentionKey:
        """
        内容摘要和原始身份不重写，只替换本次新增的虚拟 checkpoint。

        :param original (RetentionKey): 原始保护图键
        :return RetentionKey: 与实际发布可直接比较的键
        """
        if original.kind in {"checkpoint", "directory_context"}:
            identity = UUID(original.identity)
            return RetentionKey(original.kind, str(identities.get(identity, identity)))
        return original

    return RetentionProtection(
        roots={key(k): reasons for k, reasons in predicted.after.roots.items()},
        protected={key(k): reasons for k, reasons in predicted.after.protected.items()},
        directory_members=tuple(
            replace(
                row,
                directory_checkpoint_id=identities.get(
                    row.directory_checkpoint_id, row.directory_checkpoint_id
                ),
                checkpoint_id=identities.get(row.checkpoint_id, row.checkpoint_id),
            )
            for row in predicted.after.directory_members
        ),
    )


@pytest.mark.parametrize("linked", [False, True])
async def test_projection_matches_real_publication_without_preview_writes(
    stopped: RuntimeHarness, tmp_path: Path, linked: bool
) -> None:
    """
    可整理与链接阻断两种完整图均等于真实提交结果，预览提交不留下任何持久化改动。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    :param linked (bool): 是否保留跨根链接依赖
    """
    old, keep = await shared_directory(stopped, tmp_path, linked=linked)
    plan = await preview(stopped, tmp_path, old)
    before = await fingerprint(stopped)
    lower = datetime.now(UTC)
    predicted = await projected(stopped, tmp_path, plan)
    upper = datetime.now(UTC)
    assert await fingerprint(stopped) == before
    repeated = await projected(stopped, tmp_path, plan)
    assert repeated.projection == predicted.projection and repeated.after == predicted.after
    released = {row.key: row for row in predicted.newly_released}
    if linked:
        assert not released and predicted.before == predicted.after
    else:
        for identity in (keep, plan.directory_head_id):
            history = released[RetentionKey("checkpoint", str(identity))]
            assert history.released_at is not None and lower <= history.released_at <= upper
            assert history.expires_at == history.released_at + timedelta(days=30)
        async with stopped.database() as session:
            index = await SkillRetentionRepository(session).load(stopped.owner)
            with pytest.raises(SkillContentError) as error:
                require_checkpoint_dependencies(index, {RetentionKey("checkpoint", str(old))})
            assert error.value.code == "HISTORY_REFERENCED"
    result = await apply(stopped, tmp_path, plan)
    async with stopped.database() as session:
        actual = await SkillRetentionInspector(session).inspect(stopped.owner)
        assert mapped_protection(predicted, result) == actual


async def test_projection_revalidates_pin_and_owner_before_analysis(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    重验拒绝新 pin 和另一用户，不能用过期计划获取假设删除资格。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    plan = await preview(stopped, tmp_path, old)
    async with stopped.database() as session:
        service = SkillDirectoryCompactionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        with pytest.raises(SkillContentError) as error:
            await service.retention_preview(uuid4(), plan)
        assert error.value.code == "ACCOUNT_NOT_FOUND"
        checkpoint = await session.get(SkillCheckpoint, old)
        assert checkpoint is not None
        branch = await session.get(AccountSkillState, checkpoint.state_id)
        assert branch is not None
        revision = branch.base_revision_id
    await pin(stopped, tmp_path, revision)
    before = await fingerprint(stopped)
    with pytest.raises(SkillContentError) as error:
        await projected(stopped, tmp_path, plan)
    assert error.value.code == "STATE_PROTECTED"
    assert await fingerprint(stopped) == before


async def test_projection_forecasts_new_release_but_preserves_unknown_history(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    陈旧 release 不可让新解除保护的 head 立即过期；未知历史保持未知，投影不写时钟。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, keep = await shared_directory(stopped, tmp_path)
    stale = datetime.now(UTC) - timedelta(days=365)
    async with stopped.database.begin() as session:
        original = await session.get(SkillCheckpoint, old)
        current = await session.get(SkillCheckpoint, keep)
        assert original is not None and current is not None
        original.retention_released_at = None
        current.retention_released_at = stale
    plan = await preview(stopped, tmp_path, old)
    before = await fingerprint(stopped)
    predicted = await projected(stopped, tmp_path, plan)
    histories = {row.key: row for row in predicted.history}
    unknown = histories[RetentionKey("checkpoint", str(old))]
    assert unknown.released_at is None and unknown.expires_at is None
    released = histories[RetentionKey("checkpoint", str(keep))]
    assert released.released_at is not None and released.released_at > stale
    assert released.expires_at == released.released_at + timedelta(days=30)
    assert await fingerprint(stopped) == before


async def test_projection_rejects_virtual_identity_collision_and_cross_owner_index(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    不允许虚拟节点与真实 checkpoint 合并，也不接受内部错误拼接的所有者索引。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    plan = await preview(stopped, tmp_path, old)
    predicted = await projected(stopped, tmp_path, plan)
    async with stopped.database() as session:
        index = await SkillRetentionRepository(session).load(stopped.owner)
        collision = SkillCheckpoint(id=predicted.projection.checkpoints[0].id)
        with pytest.raises(ValueError, match="identity collision"):
            project_retention(
                replace(index, checkpoints=(*index.checkpoints, collision)),
                plan,
                datetime.now(UTC),
                SkillStoragePolicy(),
            )
        with pytest.raises(ValueError, match="owner mismatch"):
            project_retention(
                replace(index, user_id=uuid4()), plan, datetime.now(UTC), SkillStoragePolicy()
            )


async def test_projection_keeps_archive_deadline_for_released_complete_directory(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    完整旧目录和同一归档会话的旧视图均预计九十天等待，不按保留项名称缩短。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    from test_skill_library import LibraryHarness

    from agent_remote_server.schemas.skill_library import SkillRemoveRequest

    old, keep = await shared_directory(stopped, tmp_path)
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRemoveRequest(
            skill="learning",
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    plan = await preview(stopped, tmp_path, old)
    predicted = await projected(stopped, tmp_path, plan)
    histories = {row.key: row for row in predicted.newly_released}
    directory = histories[RetentionKey("checkpoint", str(plan.directory_head_id))]
    item = histories[RetentionKey("checkpoint", str(keep))]
    assert directory.archived and item.archived
    assert directory.released_at is not None and item.released_at is not None
    assert directory.expires_at == directory.released_at + timedelta(days=90)
    assert item.expires_at == item.released_at + timedelta(days=90)
    result = await apply(stopped, tmp_path, plan)
    async with stopped.database() as session:
        assert mapped_protection(predicted, result) == await SkillRetentionInspector(
            session
        ).inspect(stopped.owner)


async def test_projection_preserves_shared_blob_upload_lease_after_root_removal(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    状态目录移除根后，另一配额分类的活动上传仍保护同一物理文件且不反向保活历史身份。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    """
    from test_skill_content_service import service

    old, _ = await shared_directory(stopped, tmp_path)
    plan = await preview(stopped, tmp_path, old)
    directory = plan.directories[0]
    removed_file = next(row for row in directory.original.entries if row.path == "learning/memory")
    async with stopped.database.begin() as session:
        await service(session, tmp_path).begin(
            stopped.owner, str(uuid4()), directory.original, "package"
        )
    plan = await preview(stopped, tmp_path, old)
    predicted = await projected(stopped, tmp_path, plan)
    assert predicted.after.reasons("blob", removed_file.sha256) == {"upload_lease"}
    assert predicted.after.reasons("package_object", removed_file.sha256) == {"upload_lease"}
    assert not predicted.after.reasons("checkpoint", old)
    result = await apply(stopped, tmp_path, plan)
    async with stopped.database() as session:
        assert mapped_protection(predicted, result) == await SkillRetentionInspector(
            session
        ).inspect(stopped.owner)


async def test_projection_keeps_active_snapshot_exact_backing_after_head_replacement(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    新版会话仍引用旧 helper backing 时，整理可替换当前 head 但不能释放该精确输入。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    """
    from skill_publication_support import new_session
    from test_skill_state_commands import command, execute

    old, keep = await shared_directory(stopped, tmp_path)
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    await new_session(stopped, tmp_path)
    plan = await preview(stopped, tmp_path, old)
    predicted = await projected(stopped, tmp_path, plan)
    assert "active_snapshot" in predicted.after.reasons("checkpoint", keep)
    assert any(row.original_id == keep for row in predicted.projection.checkpoints)
    assert RetentionKey("checkpoint", str(keep)) not in {
        row.key for row in predicted.newly_released
    }
    result = await apply(stopped, tmp_path, plan)
    async with stopped.database() as session:
        assert mapped_protection(predicted, result) == await SkillRetentionInspector(
            session
        ).inspect(stopped.owner)


async def test_projection_keeps_legacy_head_and_rejects_changed_plan(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    无 backing 的历史 head 不能被推测替换，改变原确认代数也不能进入保护推演。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, keep = await shared_directory(stopped, tmp_path)
    async with stopped.database.begin() as session:
        checkpoint = await session.get(SkillCheckpoint, keep)
        assert checkpoint is not None
        checkpoint.backing_directory_id = None
    plan = await preview(stopped, tmp_path, old)
    predicted = await projected(stopped, tmp_path, plan)
    assert predicted.after.reasons("checkpoint", keep)
    assert all(row.original_id != keep for row in predicted.projection.checkpoints)
    with pytest.raises(SkillContentError) as error:
        await projected(
            stopped, tmp_path, replace(plan, library_generation=plan.library_generation + 1)
        )
    assert error.value.code == "HEAD_CHANGED"
    result = await apply(stopped, tmp_path, plan)
    async with stopped.database() as session:
        assert mapped_protection(predicted, result) == await SkillRetentionInspector(
            session
        ).inspect(stopped.owner)
