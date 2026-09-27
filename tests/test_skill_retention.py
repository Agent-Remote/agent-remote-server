"""
验证真实账户流程的保活闭包、历史退役边界与只读隔离。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from deployment_attempt_support import observe
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_content_service import user
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_local import activate, register, source
from test_skill_migration import migrate, request, versions
from test_skill_migration_conflicts import pending as migration_pending
from test_skill_preparation import pin
from test_skill_resolution_service import choose, pending, upload_tree
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command, execute
from test_skill_storage import file_entry

from agent_remote_server.models.skill_library import SkillInstallation
from agent_remote_server.models.skill_snapshots import SkillFinalization
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.models.skill_storage import SkillContentUpload, SkillStorageUsage
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.retention import SkillRetentionInspector
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.skill_manager.retention.graph import RetentionProtection


async def inspect(state: RuntimeHarness) -> RetentionProtection:
    """
    每次重建服务和只读请求事务，避免内存对象充当引用权威。

    :param state (RuntimeHarness): 已持久化账户身份
    :return RetentionProtection: 当前完整保护闭包
    """
    async with state.database() as session:
        return await SkillRetentionInspector(session).inspect(state.owner)


async def test_retention_inspection_is_owner_scoped_and_write_free(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    未使用用户无根且不创建计量行，同摘要其他用户的身份不泄漏。

    :param stopped (RuntimeHarness): 已使用账户
    :param tmp_path (Path): 私有内容卷
    """
    outsider = await user(stopped.database)
    async with stopped.database.begin() as session:
        empty = await SkillRetentionInspector(session).inspect(outsider)
        assert not empty.roots and not empty.protected and not empty.directory_members
        assert await session.get(SkillStorageUsage, outsider) is None
    other = LibraryHarness(stopped.database, tmp_path, outsider)
    await other.add(await other.candidate())
    other_item = await other.info()
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None
        before = (usage.lock_version, usage.state_bytes, usage.package_bytes)
        first = await SkillRetentionInspector(session).inspect(stopped.owner)
        assert not first.reasons("revision", other_item.default_revision_id or UUID(int=0))
    assert await inspect(stopped) == first
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None
        assert (usage.lock_version, usage.state_bytes, usage.package_bytes) == before
        with pytest.raises(ValueError, match="row limit exceeded"):
            await SkillRetentionRepository(session, max_rows=1).load(stopped.owner)
        with pytest.raises(ValueError, match="metadata budget"):
            await SkillRetentionRepository(session, max_json_characters=1).load(stopped.owner)


async def test_current_disabled_branch_and_pins_remain_protected(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    停用不解除当前解析版本保护，被覆盖的 pin 仍保留其精确包与分支。

    :param stopped (RuntimeHarness): 初始会话
    :param tmp_path (Path): 内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    source_revision, target = await versions(stopped, tmp_path)
    await pin(stopped, tmp_path, source_revision)
    selected = (await command(stopped, tmp_path)).expected.targets[0]
    async with stopped.database.begin() as session:
        item = await session.get(SkillInstallation, selected.skill_id)
        assert item is not None
        item.default_enabled = False
    result = await inspect(stopped)
    assert {"current_branch", "pin"} <= result.reasons("branch", stopped.state)
    assert "library_default" in result.reasons("revision", target)
    assert "pin" in result.reasons("revision", source_revision)


async def test_old_head_and_parent_do_not_become_permanent_roots(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    已收尾旧分支只有目录物化义务，普通 parent 不保活全部历史。

    :param stopped (RuntimeHarness): 初始分支
    :param tmp_path (Path): 私有卷
    """
    initial = (await command(stopped, tmp_path)).expected.targets[0].head_checkpoint_id
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"old"}))
    current = (await command(stopped, tmp_path)).expected.targets[0].head_checkpoint_id
    assert initial is not None and current is not None and initial != current
    protected = await inspect(stopped)
    assert protected.reasons("checkpoint", current)
    assert not protected.reasons("checkpoint", initial)
    await versions(stopped, tmp_path)
    historical = await inspect(stopped)
    assert not historical.reasons("branch", stopped.state)
    assert not historical.reasons("checkpoint", current)
    assert any(
        member.state_id == stopped.state and member.checkpoint_id == current
        for member in historical.directory_members
    )


async def test_active_snapshot_and_pending_finalization_keep_original_and_live_heads(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    终态 session 尚未收尾也保护基线，完整上传未发布继续由输入保护。

    :param stopped (RuntimeHarness): 尚未上传的已停止会话
    :param tmp_path (Path): 内容卷
    """
    result = await inspect(stopped)
    assert "active_snapshot" in result.reasons("snapshot", stopped.snapshot)
    assert "active_snapshot" in result.reasons("branch", stopped.state)
    identity = await ingest(stopped, tmp_path, {"learning/late": b"pending"})
    result = await inspect(stopped)
    assert result.reasons("finalization", identity) == {"pending_finalization"}
    assert "pending_finalization" in result.reasons("snapshot", stopped.snapshot)
    await publish(stopped, tmp_path, identity)
    result = await inspect(stopped)
    assert not result.reasons("finalization", identity)
    assert not result.reasons("snapshot", stopped.snapshot)


async def test_publication_conflict_and_custom_choice_keep_complete_inputs_until_reset(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    未解决三侧与人工树始终保活；reset 后旧回执存在但不再成为硬根。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    conflict = await pending(stopped, tmp_path)
    custom = await upload_tree(stopped, tmp_path, {"content": b"resolved"})
    result = await choose(
        stopped,
        tmp_path,
        conflict,
        SkillResolutionChoice(path="learning/one", file_tree_digest=custom),
    )
    assert result.status == "pending"
    protected = await inspect(stopped)
    assert protected.reasons("publication", conflict.id) == {"publication_conflict"}
    assert "publication_conflict" in protected.reasons("state_tree", custom)
    assert "publication_conflict" in protected.reasons(
        "state_tree", conflict.current_tree_digest or ""
    )
    async with stopped.database() as session:
        finalization = await session.get(SkillFinalization, conflict.finalization_id)
        assert finalization is not None
        assert "publication_conflict" in protected.reasons("snapshot", finalization.snapshot_id)
        assert "publication_conflict" in protected.reasons(
            "checkpoint", finalization.checkpoint_id or UUID(int=0)
        )
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    stale = await inspect(stopped)
    assert not stale.reasons("publication", conflict.id)
    assert not stale.reasons("state_tree", custom)


@pytest.mark.parametrize("mode", ["forward", "incremental"])
async def test_migration_conflict_keeps_all_saved_sides_and_branches(
    stopped: RuntimeHarness, tmp_path: Path, mode: str
) -> None:
    """
    准备与显式迁移使用独立冲突根，不能因目标规则变化丢掉完整比较。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param mode (str): 冲突迁移类型
    """
    original = await migration_pending(stopped, tmp_path, mode)
    assert original.operation_id is not None
    result = await inspect(stopped)
    assert result.reasons("migration", original.operation_id) == {"migration_conflict"}
    for digest in (original.base_digest, original.current_digest, original.incoming_digest):
        assert "migration_conflict" in result.reasons("state_tree", digest)
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    assert not (await inspect(stopped)).reasons("migration", original.operation_id)


async def test_latest_incremental_baseline_survives_but_replaced_baseline_and_epoch_do_not(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    精确 last-migrated 保活，不将每次成功比较和旧纪元永久保活。

    :param stopped (RuntimeHarness): 旧版会话
    :param tmp_path (Path): 内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"one"}))
    late = await new_session(stopped, tmp_path)
    source_revision, target = await versions(stopped, tmp_path)
    first = await migrate(
        stopped, tmp_path, await request(stopped, tmp_path, source_revision, target)
    )
    assert first.operation_id is not None
    baseline = first.before.source.checkpoint_id
    assert baseline is not None
    initial = await inspect(stopped)
    assert initial.reasons("migration_baseline", first.operation_id)
    assert "current_branch" in initial.reasons("checkpoint", baseline)
    await publish(late, tmp_path, await ingest(late, tmp_path, {"learning/new": b"two"}))
    second = await migrate(
        stopped, tmp_path, await request(stopped, tmp_path, source_revision, target)
    )
    assert second.operation_id is not None
    latest = await inspect(stopped)
    assert latest.reasons("migration_baseline", second.operation_id)
    assert not latest.reasons("migration_baseline", first.operation_id)
    assert not latest.reasons("checkpoint", baseline)
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    assert not (await inspect(stopped)).reasons("migration_baseline", second.operation_id)


async def test_disabled_local_initial_snapshot_remains_available_for_reset(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    账户本地停用项不从 active 查询消失后失去其初始内容保护。

    :param stopped (RuntimeHarness): 所有者账户
    :param tmp_path (Path): 私有内容卷
    """
    initial = await source(stopped, tmp_path, linked=True)
    item = await register(stopped, tmp_path, initial)
    await activate(stopped, item, enabled=False)
    assert item.default_revision_id is not None
    result = await inspect(stopped)
    assert "local_original" in result.reasons("checkpoint", initial.id)
    assert "local_original" in result.reasons("local_revision", item.default_revision_id)
    assert "local_original" in result.reasons("state_tree", initial.content_digest)


async def test_upload_expiry_uses_fixed_time_and_completed_upload_is_not_permanent_root(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    活动租约保护实际摘要，过期或完成的上传身份不会永久保护无人引用的树。

    :param stopped (RuntimeHarness): 原始所有者
    :param tmp_path (Path): 内容卷
    """
    entry = file_entry(b"unique staged bytes", path="content")
    assert entry.sha256 is not None
    async with stopped.database.begin() as session:
        upload = await content_service(session, tmp_path).begin(
            stopped.owner, str(uuid4()), SkillTreeManifest(entries=(entry,)), "state"
        )
        identity = upload.id
    async with stopped.database() as session:
        index = await SkillRetentionRepository(session).load(stopped.owner)
        now = datetime.now(UTC)
        result = protection(index, now)
        assert result.reasons("upload", identity) == {"upload_lease"}
        assert "upload_lease" in result.reasons("blob", entry.sha256)
        assert not protection(index, now + timedelta(days=2)).reasons("upload", identity)
        with pytest.raises(ValueError, match="aware timestamp"):
            protection(index, now.replace(tzinfo=None))
    custom = await upload_tree(stopped, tmp_path, {"content": b"unreferenced complete"})
    result = await inspect(stopped)
    assert not result.reasons("state_tree", custom)
    async with stopped.database() as session:
        retained = await session.get(SkillContentUpload, identity)
        assert retained is not None and retained.status == "staged"


async def test_removed_installation_preserves_explicit_package_pin_but_not_archived_head(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    卸载保留的 pin 保护版本包，但旧安装 head 不因残存字段永久保活。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    revision = (await command(stopped, tmp_path)).expected.targets[0].revision_id
    await pin(stopped, tmp_path, revision)
    selected = (await command(stopped, tmp_path)).expected.targets[0]
    async with stopped.database.begin() as session:
        item = await session.get(SkillInstallation, selected.skill_id)
        assert item is not None
        item.removed = True
    result = await inspect(stopped)
    assert "pin" in result.reasons("revision", revision)
    assert not result.reasons("branch", stopped.state)
    async with stopped.database() as session:
        branch = await session.scalar(
            select(AccountSkillState).where(AccountSkillState.id == stopped.state)
        )
        assert branch is not None and branch.head_checkpoint_id is not None


async def test_pending_operation_roots_are_exact_and_unknown_shapes_fail_closed(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    未结束操作按保存版本保活，未知状态或缺失目标不能产生看似完整的回收分析。

    :param stopped (RuntimeHarness): 已安装用户
    :param tmp_path (Path): 私有卷
    """
    from agent_remote_server.models.skill_deployment import SkillDeploymentTarget
    from agent_remote_server.models.skill_library import SkillOperation

    await versions(stopped, tmp_path)
    async with stopped.database.begin() as session:
        operation = await session.scalar(
            select(SkillOperation)
            .join(SkillDeploymentTarget, SkillDeploymentTarget.operation_id == SkillOperation.id)
            .where(SkillOperation.user_id == stopped.owner)
        )
        assert operation is not None
        await observe(session, operation, "pending")
        identity = operation.id
        data = operation.result_json
        revisions = data["revision_ids"]
        assert isinstance(revisions, list) and revisions
    result = await inspect(stopped)
    assert result.reasons("operation", identity) == {"pending_operation"}
    for revision in revisions:
        assert "pending_operation" in result.reasons("revision", str(revision))
    async with stopped.database.begin() as session:
        operation = await session.get(SkillOperation, identity)
        assert operation is not None
        operation.status = "new_unclassified_state"
    with pytest.raises(ValueError, match="unclassified"):
        await inspect(stopped)
    async with stopped.database.begin() as session:
        operation = await session.get(SkillOperation, identity)
        assert operation is not None
        operation.status = "preparing"
        operation.result_json = {**data, "targets": []}
    with pytest.raises(ValueError, match="inventory"):
        await inspect(stopped)


async def test_postgres_inspection_holds_same_user_lock_against_new_reference(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    独立 PostgreSQL 连接证明分析事务与新版本引用串行，而非依赖同一 ORM 会话。

    :param stopped (RuntimeHarness): 已发布来源账户
    :param tmp_path (Path): 私有卷
    """
    import asyncio

    from sqlalchemy import func

    from agent_remote_server.repositories.skill_storage import SkillStorageRepository

    async with stopped.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    source_revision, _ = await versions(stopped, tmp_path)
    selected = (await command(stopped, tmp_path)).expected.targets[0]
    ready = asyncio.Event()
    writer_pid: list[int] = []

    async def write_reference() -> None:
        """
        在独立事务取得同一用户锁后写入明确来源版本引用。
        """
        async with stopped.database.begin() as writer:
            writer_pid.append(int(await writer.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            await SkillStorageRepository(writer).lock_usage(stopped.owner)
            item = await writer.get(SkillInstallation, selected.skill_id)
            assert item is not None
            item.default_revision_id = source_revision

    task: asyncio.Task[None] | None = None
    try:
        async with stopped.database() as reader:
            original = await SkillRetentionInspector(reader).inspect(stopped.owner)
            assert not original.reasons("branch", stopped.state)
            task = asyncio.create_task(write_reference())
            await asyncio.wait_for(ready.wait(), timeout=2)
            blocked = False
            async with stopped.database() as observer:
                for _ in range(100):
                    blockers = await observer.scalar(select(func.pg_blocking_pids(writer_pid[0])))
                    if blockers:
                        blocked = True
                        break
                    await asyncio.sleep(0.01)
            assert blocked and not task.done()
    finally:
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
    assert "current_branch" in (await inspect(stopped)).reasons("branch", stopped.state)
