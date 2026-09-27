"""
验证真实树引用释放、独立配额、上传保护和持久化标记的原子边界。
"""

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database as database
from test_skill_content_service import service, user
from test_skill_library import LibraryHarness
from test_skill_storage import file_entry

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.models.skill_storage import (
    SkillContentObject,
    SkillContentUpload,
    SkillStorageUsage,
    SkillStoredTree,
)
from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.schemas.skill_library import SkillAddRequest
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.gc import SkillContentReclamationService
from agent_remote_server.services.skills.gc.planning import ContentReclamationPlan
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import ContentScope, SkillStoragePolicy


async def upload(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    owner: UUID,
    scope: ContentScope = "state",
    data: bytes = b"retained content",
    path: str = "SKILL.md",
) -> tuple[UUID, RetentionKey, str]:
    """
    用真实内容服务完成私有树，返回原受理和精确内容身份。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 内容卷
    :param owner (UUID): 内容所有者
    :param scope (ContentScope): 上传类别
    :param data (bytes): 完整文件内容
    :param path (str): 清单路径
    :return tuple[UUID, RetentionKey, str]: 原上传、分类树和文件摘要
    """
    entry = file_entry(data, path=path)
    async with database.begin() as session:
        svc = service(session, tmp_path)
        accepted = await svc.begin(owner, str(uuid4()), SkillTreeManifest(entries=(entry,)), scope)
        await svc.put_file(owner, accepted.id, entry.sha256, io.BytesIO(data))
        tree = await svc.complete(owner, accepted.id)
        return (
            accepted.id,
            RetentionKey("package_tree" if scope == "package" else "state_tree", tree.digest),
            entry.sha256,
        )


async def preview(
    database: async_sessionmaker[AsyncSession],
    owner: UUID,
    *trees: RetentionKey,
    objects: tuple[RetentionKey, ...] = (),
    early: bool = True,
) -> ContentReclamationPlan:
    """
    通过独立只读事务取得精确计划，预览不得产生任何 ORM 变更。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param owner (UUID): 用户
    :param trees (RetentionKey): 选定树
    :param objects (tuple[RetentionKey, ...]): 额外显式对象
    :param early (bool): 是否明确提前回收
    :return ContentReclamationPlan: 完整预览
    """
    async with database.begin() as session:
        result = await SkillContentReclamationService(session, SkillStoragePolicy()).preview(
            owner,
            trees,
            objects=objects,
            all_unreferenced=early,
        )
        assert not session.new and not session.dirty and not session.deleted
        return result


async def test_tree_release_wait_and_durable_marker_accounting(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    普通树等待真实到期后才解除，逻辑额度与标记原子提交但不声称磁盘已删。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 私有内容卷
    """
    owner = await user(database)
    identity, key, digest = await upload(database, tmp_path, owner)
    plan = await preview(database, owner, key, early=False)
    assert not plan.ready and plan.state_bytes == plan.pending_file_bytes == 0
    async with database.begin() as session:
        with pytest.raises(SkillContentError, match="blockers"):
            await SkillContentReclamationService(session, SkillStoragePolicy()).apply(owner, plan)
        tree = await session.get(SkillStoredTree, (owner, "state", key.identity))
        assert tree is not None
        tree.retention_released_at = datetime.now(UTC) - timedelta(days=31)
    plan = await preview(database, owner, key, early=False)
    assert plan.ready and plan.state_bytes == len(b"retained content")
    assert plan.pending_file_bytes == plan.state_bytes
    async with database.begin() as session:
        result = await SkillContentReclamationService(session, SkillStoragePolicy()).apply(
            owner, plan
        )
        assert len(result.deletion_ids) == 1
    async with database.begin() as session:
        assert await session.get(SkillStoredTree, (owner, "state", key.identity)) is None
        usage = await session.get(SkillStorageUsage, owner)
        marker = await session.get(SkillContentObject, (owner, "state", digest))
        task = await session.get(SkillContentDeletion, result.deletion_ids[0])
        assert usage is not None and usage.state_bytes == 0
        assert marker is not None and marker.status == "deleting"
        assert task is not None and task.status == "pending" and task.category_mask == 2
        assert (await service(session, tmp_path).get(owner, identity)).status == "committed"
        with pytest.raises(SkillContentError) as error:
            await service(session, tmp_path).complete(owner, identity)
        assert error.value.code == "CONTENT_EXPIRED"
    assert (tmp_path / "objects" / str(owner) / digest[:2] / digest).is_file()


@pytest.mark.parametrize("both", [False, True])
async def test_shared_file_category_release_is_distinct_from_physical_deletion(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    both: bool,
) -> None:
    """
    一份共享文件按分类独立结算，两分类同时释放才登记一次物理删除。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    :param both (bool): 是否明确同时释放两分类
    """
    owner = await user(database)
    _, state, digest = await upload(database, tmp_path, owner)
    _, package, _ = await upload(database, tmp_path, owner, "package")
    plan = await preview(database, owner, *((state, package) if both else (state,)))
    size = len(b"retained content")
    assert plan.state_bytes == size and plan.package_bytes == (size if both else 0)
    assert plan.pending_file_bytes == (size if both else 0)
    async with database.begin() as session:
        result = await SkillContentReclamationService(session, SkillStoragePolicy()).apply(
            owner, plan
        )
        assert len(result.deletion_ids) == int(both)
    if not both:
        async with database() as session:
            assert await session.get(SkillContentObject, (owner, "state", digest)) is None
            manifest = await service(session, tmp_path).read_tree(
                owner, "package", package.identity
            )
            assert manifest.entries[0].sha256 == digest


async def test_retained_same_category_tree_prevents_quota_release(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    相同对象仍被另一完整树引用时，不能用已删树的总长度结算。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    _, selected, _ = await upload(database, tmp_path, owner)
    _, remaining, _ = await upload(database, tmp_path, owner, path="other")
    plan = await preview(database, owner, selected)
    assert plan.ready and plan.state_bytes == plan.pending_file_bytes == 0
    async with database.begin() as session:
        await SkillContentReclamationService(session, SkillStoragePolicy()).apply(owner, plan)
        assert await session.get(SkillStoredTree, (owner, "state", remaining.identity)) is not None


@pytest.mark.parametrize("opposite", [False, True])
async def test_active_upload_preserves_correct_category_and_future_completion(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    opposite: bool,
) -> None:
    """
    零预留同分类保留额度，另一分类活动上传只保留文件；两者都能随后完整完成。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    :param opposite (bool): 上传是否属于另一分类
    """
    owner = await user(database)
    _, tree, digest = await upload(database, tmp_path, owner)
    async with database.begin() as session:
        svc = service(session, tmp_path)
        manifest = await svc.read_tree(owner, "state", tree.identity)
        pending = await svc.begin(owner, str(uuid4()), manifest, "package" if opposite else "state")
        assert pending.reserved_bytes == (manifest.total_bytes if opposite else 0)
        upload_id = pending.id
    plan = await preview(database, owner, tree)
    assert plan.pending_file_bytes == 0
    assert plan.state_bytes == (manifest.total_bytes if opposite else 0)
    async with database.begin() as session:
        await SkillContentReclamationService(session, SkillStoragePolicy()).apply(owner, plan)
        assert not list(
            await session.scalars(
                select(SkillContentDeletion).where(SkillContentDeletion.user_id == owner)
            )
        )
    async with database.begin() as session:
        completed = await service(session, tmp_path).complete(owner, upload_id)
        assert completed.digest == tree.identity
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None
        assert (usage.package_bytes, usage.state_bytes) == (
            (manifest.total_bytes, 0) if opposite else (0, manifest.total_bytes)
        )
        assert completed.manifest_json["entries"]
        assert (
            await session.get(
                SkillContentObject, (owner, "package" if opposite else "state", digest)
            )
            is not None
        )


async def test_expired_zero_reservation_lease_allows_explicit_orphan_object_cleanup(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    上次因零预留租约暂留的无树对象可在租约结束后显式结算，不额外等待或丢失额度。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    _, tree, digest = await upload(database, tmp_path, owner)
    async with database.begin() as session:
        svc = service(session, tmp_path)
        pending = await svc.begin(
            owner, str(uuid4()), await svc.read_tree(owner, "state", tree.identity), "state"
        )
        assert pending.reserved_bytes == 0
        upload_id = pending.id
    plan = await preview(database, owner, tree)
    async with database.begin() as session:
        await SkillContentReclamationService(session, SkillStoragePolicy()).apply(owner, plan)
        saved_upload = await session.get(SkillContentUpload, upload_id)
        assert saved_upload is not None
        saved_upload.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    plan = await preview(database, owner, objects=(RetentionKey("state_object", digest),))
    assert plan.ready and plan.state_bytes == len(b"retained content")
    async with database.begin() as session:
        result = await SkillContentReclamationService(session, SkillStoragePolicy()).apply(
            owner, plan
        )
        assert result.deletion_ids
        with pytest.raises(SkillContentError) as error:
            await service(session, tmp_path).complete(owner, upload_id)
        assert error.value.code == "UPLOAD_NOT_ACTIVE"


async def test_plan_change_and_post_flush_failure_preserve_whole_selection(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    新租约使原计划失效；实际任务写入后失败，即使调用方捕获并提交仍整体回滚。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 保存点故障注入
    """
    owner = await user(database)
    _, tree, digest = await upload(database, tmp_path, owner)
    plan = await preview(database, owner, tree)
    _, other, _ = await upload(database, tmp_path, owner, path="other")
    async with database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillContentReclamationService(session, SkillStoragePolicy()).apply(owner, plan)
        assert error.value.code == "HEAD_CHANGED"
    plan = await preview(database, owner, tree, other)
    original = SkillContentGCRepository.flush
    calls = 0

    async def fail_after_flush(repository: SkillContentGCRepository) -> None:
        """
        让真实任务和标记先 flush，然后在同一业务保存点失败。

        :param repository (SkillContentGCRepository): 当前仓储
        """
        nonlocal calls
        await original(repository)
        calls += 1
        if calls == 2:
            raise RuntimeError("post flush failure")

    monkeypatch.setattr(SkillContentGCRepository, "flush", fail_after_flush)
    async with database.begin() as session:
        with pytest.raises(RuntimeError, match="post flush"):
            await SkillContentReclamationService(session, SkillStoragePolicy()).apply(owner, plan)
    async with database() as session:
        assert await session.get(SkillStoredTree, (owner, "state", tree.identity)) is not None
        assert await session.get(SkillStoredTree, (owner, "state", other.identity)) is not None
        obj = await session.get(SkillContentObject, (owner, "state", digest))
        usage = await session.get(SkillStorageUsage, owner)
        assert obj is not None and obj.status == "available"
        assert usage is not None and usage.state_bytes == len(b"retained content")
        assert not list(
            await session.scalars(
                select(SkillContentDeletion).where(SkillContentDeletion.user_id == owner)
            )
        )


async def test_historical_foreign_keys_and_owner_scope_block_whole_plan(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    原始包外键与当前保护不能通过提前模式绕过，错误所有者不能使用他人的合法计划。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    library = LibraryHarness(database, tmp_path, owner)
    candidate = await library.candidate()
    await library.execute(
        SkillAddRequest(
            items=(candidate,),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    _, free, _ = await upload(database, tmp_path, owner)
    package = RetentionKey("package_tree", candidate.tree_digest)
    plan = await preview(database, owner, free, package)
    assert not plan.ready and any("retained_history" in reasons for _, reasons in plan.blockers)
    async with database.begin() as session:
        svc = SkillContentReclamationService(session, SkillStoragePolicy())
        with pytest.raises(SkillContentError):
            await svc.apply(owner, plan)
        with pytest.raises(SkillContentError):
            await svc.apply(uuid4(), plan)
        assert await session.get(SkillStoredTree, (owner, "state", free.identity)) is not None


async def test_zero_reservation_upload_invalidates_original_reclamation_plan(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    新增零预留租约即使未增加额度也使原删除确认失效，不能沿用此前无上传的计划。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    _, tree, _ = await upload(database, tmp_path, owner)
    plan = await preview(database, owner, tree)
    async with database.begin() as session:
        svc = service(session, tmp_path)
        pending = await svc.begin(
            owner, str(uuid4()), await svc.read_tree(owner, "state", tree.identity), "state"
        )
        assert pending.reserved_bytes == 0
    async with database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillContentReclamationService(session, SkillStoragePolicy()).apply(owner, plan)
        assert error.value.code == "HEAD_CHANGED"
        assert await session.get(SkillStoredTree, (owner, "state", tree.identity)) is not None
