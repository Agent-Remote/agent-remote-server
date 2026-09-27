"""
验证完整树的真实等待、原上传重放、引用库存和失败回滚，不把树时钟当作删除授权。
"""

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import ingest, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_checkpoint_retirement import historical_selection
from test_skill_content_service import database as database
from test_skill_content_service import service, user
from test_skill_finalization import stopped as stopped
from test_skill_history_retirement import retire
from test_skill_migration import versions
from test_skill_preparation import pin
from test_skill_snapshots import prepared as prepared
from test_skill_storage import file_entry

from agent_remote_server.models.skill_library import SkillRevision
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.models.skill_storage import (
    SkillContentUpload,
    SkillStorageUsage,
    SkillStoredTree,
)
from agent_remote_server.repositories.skill_retention import (
    RetentionIndex,
    SkillRetentionRepository,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.retention import SkillRetentionInspector
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.retention.trees import StoredTreeRetention
from agent_remote_server.skill_manager.retention.graph import RetentionKey, RetentionProtection
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def trees(
    database: async_sessionmaker[AsyncSession], owner: UUID
) -> dict[RetentionKey, StoredTreeRetention]:
    """
    通过真实只读入口取得分类树的时钟和所有实际内容引用。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param owner (UUID): 已授权用户
    :return dict[RetentionKey, StoredTreeRetention]: 以分类摘要索引的保留信息
    """
    async with database.begin() as session:
        result = await SkillRetentionInspector(session).trees(owner, SkillStoragePolicy())
        assert not session.new and not session.dirty
        return {row.key: row for row in result}


async def test_unbound_tree_wait_starts_on_fresh_completion_but_not_replay(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    完整但未绑定历史的树有真实等待起点，新交付可重启等待；原重放和只读不刷新时钟。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 私有内容卷
    """
    owner = await user(database)
    data = b"unbound complete tree"
    manifest = SkillTreeManifest(entries=(file_entry(data),))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(owner, str(uuid4()), manifest, "state")
        await svc.put_file(owner, upload.id, manifest.entries[0].sha256, io.BytesIO(data))
        lower = datetime.now(UTC)
        tree = await svc.complete(owner, upload.id)
        identity, digest = upload.id, tree.digest
    key = RetentionKey("state_tree", digest)
    first = (await trees(database, owner))[key]
    assert first.released_at is not None and lower <= first.released_at <= datetime.now(UTC)
    assert first.expires_at == first.released_at + timedelta(days=30)
    assert not first.references and not first.reasons
    async with database.begin() as session:
        svc = service(session, tmp_path)
        assert (await svc.get(owner, identity)).status == "committed"
        assert (await svc.complete(owner, identity)).manifest_json == manifest.model_dump(
            mode="json"
        )
        await svc.read_tree(owner, "state", digest)
    assert (await trees(database, owner))[key] == first
    async with database.begin() as session:
        original = await session.get(SkillStoredTree, (owner, "state", digest))
        assert original is not None
        original.retention_released_at = datetime.now(UTC) - timedelta(days=40)
    async with database.begin() as session:
        svc = service(session, tmp_path)
        fresh = await svc.begin(owner, str(uuid4()), manifest, "state")
        assert fresh.reserved_bytes == 0
        lower = datetime.now(UTC)
        await svc.complete(owner, fresh.id)
    refreshed = (await trees(database, owner))[key]
    assert refreshed.released_at is not None and refreshed.released_at >= lower
    async with database() as session:
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None and usage.state_bytes == len(data) and usage.state_reserved == 0


async def test_tree_release_reacquisition_and_retained_revision_are_independent(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    最后活动会话释放旧包树后开始等待，pin 重新清空；到期也仍明确列出保留版本外键。

    :param stopped (RuntimeHarness): 原始账户及待收尾会话
    :param tmp_path (Path): 内容卷
    """
    source, _ = await versions(stopped, tmp_path)
    async with stopped.database() as session:
        revision = await session.get(SkillRevision, source)
        assert revision is not None and revision.tree_digest is not None
        key = RetentionKey("package_tree", revision.tree_digest)
    protected = (await trees(stopped.database, stopped.owner))[key]
    assert protected.reasons and protected.released_at is None
    receipt = await ingest(stopped, tmp_path, {})
    assert (await trees(stopped.database, stopped.owner))[key].released_at is None
    await publish(stopped, tmp_path, receipt)
    released = (await trees(stopped.database, stopped.owner))[key]
    assert not released.reasons and released.released_at is not None
    assert any(
        row.table == "skill_revisions" and row.identity == (str(source),)
        for row in released.references
    )
    await pin(stopped, tmp_path, source)
    assert (await trees(stopped.database, stopped.owner))[key].released_at is None
    async with stopped.database.begin() as session:
        svc = service(session, tmp_path)
        manifest = await svc.read_tree(stopped.owner, "package", key.identity)
        fresh = await svc.begin(stopped.owner, str(uuid4()), manifest, "package")
        await svc.complete(stopped.owner, fresh.id)
    assert (await trees(stopped.database, stopped.owner))[key].released_at is None
    await pin(stopped, tmp_path, None)
    latest = (await trees(stopped.database, stopped.owner))[key]
    assert latest.released_at is not None and latest.released_at > released.released_at


async def test_tree_history_retirement_releases_fks_without_restarting_tree_wait(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    退役已失去保护的完整历史只解除真实内容外键，不能强制再等待一个新保留期。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    """
    selected, head, _ = await historical_selection(stopped, tmp_path)
    async with stopped.database() as session:
        checkpoint = await session.get(SkillCheckpoint, head)
        assert checkpoint is not None
        key = RetentionKey("state_tree", checkpoint.content_digest)
    before = (await trees(stopped.database, stopped.owner))[key]
    assert not before.reasons and before.released_at is not None and before.references
    assert any(row.table == "skill_checkpoints" for row in before.references)
    await retire(stopped, *selected, early=True)
    after = (await trees(stopped.database, stopped.owner))[key]
    assert after.released_at == before.released_at and not after.references and not after.reasons
    assert after.expires_at == before.expires_at


async def test_tree_unknown_legacy_clock_survives_observation_and_unrelated_mutation(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    未知旧树不能由创建日期、只读查询或无引用变化的保存点推测释放起点。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    manifest = SkillTreeManifest(entries=())
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(owner, str(uuid4()), manifest, "state")
        tree = await svc.complete(owner, upload.id)
        digest = tree.digest
        tree.retention_released_at = None
        tree.created_at = datetime.now(UTC) - timedelta(days=400)
    before = await trees(database, owner)
    async with database.begin() as session, retention_mutation(session, owner):
        pass
    assert await trees(database, owner) == before
    legacy = before[RetentionKey("state_tree", digest)]
    assert legacy.released_at is None and legacy.expires_at is None and not legacy.references
    assert not await trees(database, uuid4())


async def test_completion_clock_failure_rolls_back_content_and_quota_after_caught_error(
    database: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    完整内容登记后的保活分析失败时，外层捕获提交也不能遗留完整树、配额或时钟。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 注入保存点末尾分析失败
    """
    from agent_remote_server.services.skills.retention import clocks

    owner = await user(database)
    data = b"rollback completion"
    manifest = SkillTreeManifest(entries=(file_entry(data),))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(owner, str(uuid4()), manifest, "state")
        await svc.put_file(owner, upload.id, manifest.entries[0].sha256, io.BytesIO(data))
        identity, digest = upload.id, upload.tree_digest
    original = clocks.protection

    def reject_created(index: RetentionIndex, now: datetime) -> RetentionProtection:
        """
        只在新树已登记后的最终分析失败，不妨碍第一次授权和字节验证。

        :param index (RetentionIndex): 当前完整引用视图
        :param now (datetime): 固定真实分析时间
        :return RetentionProtection: 未注入失败时的真实保护结果
        """
        if index.trees:
            raise ValueError("injected tree clock failure")
        return original(index, now)

    with monkeypatch.context() as patch:
        patch.setattr(clocks, "protection", reject_created)
        async with database.begin() as session:
            with pytest.raises(ValueError, match="injected tree clock"):
                await service(session, tmp_path).complete(owner, identity)
    async with database() as session:
        assert await session.get(SkillStoredTree, (owner, "state", digest)) is None
        usage = await session.get(SkillStorageUsage, owner)
        retained_upload = await session.get(SkillContentUpload, identity)
        assert usage is not None and retained_upload is not None
        assert usage.state_bytes == 0 and usage.state_reserved == len(data)
        assert retained_upload.status == "staged" and retained_upload.reserved_bytes == len(data)
    async with database.begin() as session:
        assert (await service(session, tmp_path).complete(owner, identity)).digest == digest


async def test_tree_inventory_retains_account_local_initial_revision_reference(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    未激活本地来源的初始版本仍列为真实内容引用，不能因为没有硬根就省略。

    :param prepared (RuntimeHarness): 已准备账户
    :param tmp_path (Path): 私有内容卷
    """
    from test_skill_local import register, source

    checkpoint = await source(prepared, tmp_path, linked=True)
    await register(prepared, tmp_path, checkpoint)
    async with prepared.database() as session:
        index = await SkillRetentionRepository(session).load(prepared.owner)
        original = index.local_revisions[0]
        assert original.tree_digest is not None
        key, identity = RetentionKey("state_tree", original.tree_digest), original.id
    view = (await trees(prepared.database, prepared.owner))[key]
    assert any(
        row.table == "account_local_skill_revisions"
        and row.identity == (str(identity),)
        and row.account_id == prepared.account
        for row in view.references
    )


async def test_expired_tree_wait_does_not_hide_archived_history_reference(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    普通树截止即使已到，尚在九十天归档期的精确历史仍以真实外键阻断删除。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    from test_skill_library import LibraryHarness

    from agent_remote_server.schemas.skill_library import SkillRemoveRequest

    _, head, _ = await historical_selection(stopped, tmp_path)
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRemoveRequest(
            skill="learning",
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    released = datetime.now(UTC) - timedelta(days=40)
    async with stopped.database.begin() as session:
        checkpoint = await session.get(SkillCheckpoint, head)
        assert checkpoint is not None
        checkpoint.retention_released_at = released
        tree = await session.get(
            SkillStoredTree, (stopped.owner, "state", checkpoint.content_digest)
        )
        assert tree is not None
        tree.retention_released_at = released
        key = RetentionKey("state_tree", tree.digest)
    view = (await trees(stopped.database, stopped.owner))[key]
    assert view.expires_at is not None and view.expires_at < datetime.now(UTC)
    assert any(
        row.table == "skill_checkpoints" and row.identity == (str(head),) for row in view.references
    )
    async with stopped.database() as session:
        histories = await SkillRetentionInspector(session).history(
            stopped.owner, SkillStoragePolicy()
        )
        original = next(
            row for row in histories if row.key == RetentionKey("checkpoint", str(head))
        )
        assert (
            original.archived
            and original.expires_at is not None
            and original.expires_at > datetime.now(UTC)
        )


def test_tree_reference_registry_rejects_new_fk_on_already_classified_table() -> None:
    """
    不能只登记表名后遗漏同一表新增的树内容列，实际元数据必须与完整库存逐项一致。
    """
    from sqlalchemy import Column, ForeignKey, MetaData, String

    from agent_remote_server.db import Base
    from agent_remote_server.repositories.skill_retention_schema import (
        require_classified_tree_references,
    )

    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    require_classified_tree_references(metadata)
    metadata.tables["skill_checkpoints"].append_column(
        Column("new_tree_input", String(64), ForeignKey("skill_stored_trees.digest"))
    )
    with pytest.raises(ValueError, match="unclassified skill tree"):
        require_classified_tree_references(metadata)
