"""
验证持久化 worker 的原任务重放、失败重试、磁盘先完成后的回滚和取消收尾。
"""

import asyncio
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_gc import preview, upload
from test_skill_content_service import database as database
from test_skill_content_service import service, user

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.models.skill_storage import SkillContentObject, SkillStorageUsage
from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.gc import SkillContentReclamationService
from agent_remote_server.services.skills.gc.worker import SkillContentDeletionWorker
from agent_remote_server.skill_manager.storage.filesystem import PrivateObjectFiles
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def pending_deletion(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    both: bool = False,
) -> tuple[UUID, UUID, str]:
    """
    通过真实授权计划提交标记，worker 使用独立事务读取。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有卷
    :param both (bool): 是否同时释放两分类
    :return tuple[UUID, UUID, str]: 用户、原任务和文件摘要
    """
    owner = await user(database)
    _, state, digest = await upload(database, tmp_path, owner)
    trees = [state]
    if both:
        _, package, _ = await upload(database, tmp_path, owner, "package")
        trees.append(package)
    plan = await preview(database, owner, *trees)
    async with database.begin() as session:
        result = await SkillContentReclamationService(session, SkillStoragePolicy()).apply(
            owner, plan
        )
        return owner, result.deletion_ids[0], digest


async def make_due(database: async_sessionmaker[AsyncSession], identity: UUID) -> None:
    """
    仅在测试夹具中推进持久化重试时间，不让测试等待真实退避。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param identity (UUID): 原删除任务
    """
    async with database.begin() as session:
        task = await session.get(SkillContentDeletion, identity)
        assert task is not None
        task.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)


@pytest.mark.parametrize("both", [False, True])
async def test_worker_deletes_once_and_old_task_cannot_delete_reuploaded_file(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    both: bool,
) -> None:
    """
    完成后才解除 marker；重建同摘要内容后旧任务重放和旧扫描不再执行 unlink。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    :param both (bool): 是否检验两分类同时结算
    """
    owner, identity, digest = await pending_deletion(database, tmp_path, both=both)
    worker = SkillContentDeletionWorker(database, PrivateObjectStore(tmp_path / "objects"))
    path = tmp_path / "objects" / str(owner) / digest[:2] / digest
    async with database() as session:
        assert identity in await SkillContentGCRepository(session).due(datetime.now(UTC), 1000)
    assert await worker.process(identity) == "complete"
    assert not path.exists()
    async with database() as session:
        usage = await session.get(SkillStorageUsage, owner)
        task = await session.get(SkillContentDeletion, identity)
        assert usage is not None and usage.package_bytes == usage.state_bytes == 0
        assert task is not None and task.status == "complete" and task.attempts == 1
        for category in ("package", "state"):
            assert await session.get(SkillContentObject, (owner, category, digest)) is None
    _, new_tree, _ = await upload(database, tmp_path, owner)
    assert path.read_bytes() == b"retained content"
    await PrivateObjectStore(tmp_path / "objects").delete_committed(
        owner, digest, len(b"retained content"), identity
    )
    assert path.read_bytes() == b"retained content"
    assert await worker.process(identity) == "complete"
    async with database() as session:
        assert identity not in await SkillContentGCRepository(session).due(datetime.now(UTC), 1000)
    assert path.read_bytes() == b"retained content"
    async with database() as session:
        await service(session, tmp_path).read_tree(owner, "state", new_tree.identity)
    plan = await preview(database, owner, new_tree)
    async with database.begin() as session:
        result = await SkillContentReclamationService(session, SkillStoragePolicy()).apply(
            owner, plan
        )
        new_task = result.deletion_ids[0]
        assert new_task != identity
    assert await worker.process(identity) == "complete"
    assert path.exists()
    assert await worker.process(new_task) == "complete"
    assert not path.exists()


async def test_io_failure_retains_barrier_and_retries_with_fixed_error(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    I/O 失败持久化退避但不暴露异常正文，延后期间不重复投递或开放新内容。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 磁盘错误注入
    """
    owner, identity, digest = await pending_deletion(database, tmp_path)
    store = PrivateObjectStore(tmp_path / "objects")
    original = store.delete_committed

    async def fail(user_id: UUID, file_digest: str, size: int, task_id: UUID) -> None:
        """
        模拟带私有正文的磁盘异常，正文不得进入持久化错误信息。

        :param user_id (UUID): 用户
        :param file_digest (str): 摘要
        :param size (int): 原长度
        :param task_id (UUID): 原任务
        """
        raise OSError("private filesystem details must not be persisted")

    monkeypatch.setattr(store, "delete_committed", fail)
    worker = SkillContentDeletionWorker(database, store)
    assert await worker.process(identity) == "pending"
    assert await worker.process(identity) == "not_due"
    async with database() as session:
        task = await session.get(SkillContentDeletion, identity)
        assert (
            task is not None and task.attempts == 1 and task.last_error_code == "content_io_error"
        )
        marker = await session.get(SkillContentObject, (owner, "state", digest))
        assert marker is not None and marker.status == "deleting"
    with pytest.raises(SkillContentError) as error:
        await upload(database, tmp_path, owner, "package")
    assert error.value.code == "CONTENT_UNAVAILABLE"
    monkeypatch.setattr(store, "delete_committed", original)
    await make_due(database, identity)
    assert await worker.process(identity) == "complete"
    async with database() as session:
        task = await session.get(SkillContentDeletion, identity)
        assert task is not None and task.attempts == 2 and task.last_error_code is None


async def test_unlink_followed_by_database_rollback_keeps_durable_retry_barrier(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    磁盘删除不能靠 SQL 回滚恢复；原 pending 标记必须保留到独立重试确认不存在。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 终态提交前故障
    """
    owner, identity, digest = await pending_deletion(database, tmp_path)
    worker = SkillContentDeletionWorker(database, PrivateObjectStore(tmp_path / "objects"))
    original = SkillContentGCRepository.flush

    async def fail(repository: SkillContentGCRepository) -> None:
        """
        真实终态 flush 后失败，模拟提交前进程异常。

        :param repository (SkillContentGCRepository): 独立 worker 仓储
        """
        await original(repository)
        raise RuntimeError("crash after unlink")

    monkeypatch.setattr(SkillContentGCRepository, "flush", fail)
    with pytest.raises(RuntimeError, match="crash after unlink"):
        await worker.process(identity)
    assert not (tmp_path / "objects" / str(owner) / digest[:2] / digest).exists()
    async with database() as session:
        task = await session.get(SkillContentDeletion, identity)
        marker = await session.get(SkillContentObject, (owner, "state", digest))
        assert task is not None and task.status == "pending"
        assert marker is not None and marker.status == "deleting"
    monkeypatch.setattr(SkillContentGCRepository, "flush", original)
    assert await worker.process(identity) == "complete"


async def test_worker_cancellation_waits_for_disk_thread_before_rollback(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    多次取消也不能让磁盘线程逃出用户事务，完成后回滚保留原屏障供重试。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 线程栅栏
    """
    owner, identity, digest = await pending_deletion(database, tmp_path)
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    original = PrivateObjectFiles.delete_committed

    def paused(files: PrivateObjectFiles, user_id: UUID, file_digest: str, size: int) -> None:
        """
        真正磁盘线程等待测试释放后再执行原删除。

        :param files (PrivateObjectFiles): 私有目录操作
        :param user_id (UUID): 用户
        :param file_digest (str): 摘要
        :param size (int): 原长度
        """
        entered.set()
        assert release.wait(timeout=5)
        original(files, user_id, file_digest, size)
        exited.set()

    monkeypatch.setattr(PrivateObjectFiles, "delete_committed", paused)
    worker = SkillContentDeletionWorker(database, PrivateObjectStore(tmp_path / "objects"))
    task = asyncio.create_task(worker.process(identity))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not exited.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert exited.is_set()
    async with database() as session:
        marker = await session.get(SkillContentObject, (owner, "state", digest))
        saved = await session.get(SkillContentDeletion, identity)
        assert marker is not None and marker.status == "deleting"
        assert saved is not None and saved.status == "pending"
    monkeypatch.setattr(PrivateObjectFiles, "delete_committed", original)
    assert await worker.process(identity) == "complete"


@pytest.mark.parametrize(
    "damage", ["symlink", "permissions", "size", "missing_marker", "extra_category"]
)
async def test_worker_rejects_changed_marker_or_unsafe_file(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    damage: str,
) -> None:
    """
    不一致标记或不安全文件只留下固定诊断，不删除其他文件或开放共享摘要。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    :param damage (str): 故障种类
    """
    owner, identity, digest = await pending_deletion(database, tmp_path)
    path = tmp_path / "objects" / str(owner) / digest[:2] / digest
    external = tmp_path / "external"
    external.write_bytes(b"must remain")
    if damage == "symlink":
        path.unlink()
        path.symlink_to(external)
    elif damage == "permissions":
        path.chmod(0o600)
    elif damage == "size":
        path.chmod(0o600)
        path.write_bytes(b"different size")
        path.chmod(0o400)
    else:
        async with database.begin() as session:
            marker = await session.get(SkillContentObject, (owner, "state", digest))
            assert marker is not None
            if damage == "missing_marker":
                marker.status = "available"
            else:
                session.add(
                    SkillContentObject(
                        user_id=owner,
                        category="package",
                        digest=digest,
                        size=marker.size,
                        content_kind=marker.content_kind,
                        status="available",
                    )
                )
    worker = SkillContentDeletionWorker(database, PrivateObjectStore(tmp_path / "objects"))
    assert await worker.process(identity) == "pending"
    assert os.path.lexists(path) and external.read_bytes() == b"must remain"
    async with database() as session:
        task = await session.get(SkillContentDeletion, identity)
        assert task is not None and task.status == "pending" and task.last_error_code
    assert await worker.process(uuid4()) == "missing"


@pytest.mark.parametrize("consumer", ["tree", "upload"])
async def test_worker_revalidates_real_tree_edges_and_opposite_upload_leases(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    consumer: str,
) -> None:
    """
    即使夹具绕过正常准入写入消费者，worker 也必须按完整实际引用拒绝删除。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    :param consumer (str): 违规插入的真实消费者类型
    """
    from test_skill_storage import file_entry

    from agent_remote_server.models.skill_storage import (
        SkillContentUpload,
        SkillStoredTree,
        SkillTreeObjectReference,
    )
    from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
    from agent_remote_server.skill_manager.manifest import manifest_digest

    owner, identity, digest = await pending_deletion(database, tmp_path)
    manifest = SkillTreeManifest(entries=(file_entry(b"retained content"),))
    tree_digest = manifest_digest(manifest)
    async with database.begin() as session:
        if consumer == "tree":
            session.add(
                SkillStoredTree(
                    user_id=owner,
                    category="state",
                    digest=tree_digest,
                    manifest_json=manifest.model_dump(mode="json"),
                    total_bytes=manifest.total_bytes,
                )
            )
            await session.flush()
            session.add(
                SkillTreeObjectReference(
                    user_id=owner, category="state", tree_digest=tree_digest, object_digest=digest
                )
            )
        else:
            session.add(
                SkillContentUpload(
                    user_id=owner,
                    idempotency_key=str(uuid4()),
                    scope="package",
                    tree_digest=tree_digest,
                    manifest_json=manifest.model_dump(mode="json"),
                    status="staged",
                    reserved_bytes=manifest.total_bytes,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            usage = await session.get(SkillStorageUsage, owner)
            assert usage is not None
            usage.package_reserved = manifest.total_bytes
    worker = SkillContentDeletionWorker(database, PrivateObjectStore(tmp_path / "objects"))
    assert await worker.process(identity) == "pending"
    async with database() as session:
        task = await session.get(SkillContentDeletion, identity)
        assert task is not None
        assert task.last_error_code == (
            "content_referenced" if consumer == "tree" else "content_upload_active"
        )
    assert (
        tmp_path / "objects" / str(owner) / digest[:2] / digest
    ).read_bytes() == b"retained content"


async def test_duplicate_disk_threads_serialize_and_share_original_completion_receipt(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    即使绕开 SQL 锁同时投递同一磁盘任务，卷锁串行真实线程且只执行一次原删除。

    :param database (async_sessionmaker[AsyncSession]): 独立事务
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 真实磁盘线程栅栏
    """
    from agent_remote_server.skill_manager.storage import objects as object_module

    owner, identity, digest = await pending_deletion(database, tmp_path)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    original_delete = PrivateObjectFiles.delete_committed
    original_wrapper = object_module.delete_with_receipt
    deletes: list[str] = []
    launches: list[UUID] = []

    def pause(files: PrivateObjectFiles, user_id: UUID, file_digest: str, size: int) -> None:
        """
        第一磁盘线程已经持有真实 flock，等待另一线程尝试同任务。

        :param files (PrivateObjectFiles): 私有目录操作
        :param user_id (UUID): 用户
        :param file_digest (str): 摘要
        :param size (int): 字节长度
        """
        deletes.append(file_digest)
        entered.set()
        assert release.wait(5)
        original_delete(files, user_id, file_digest, size)

    def start(
        files: PrivateObjectFiles, user_id: UUID, file_digest: str, size: int, task_id: UUID
    ) -> None:
        """
        记录第二线程确实进入字节调用，而非仅创建尚未调度的协程。

        :param files (PrivateObjectFiles): 私有目录操作
        :param user_id (UUID): 用户
        :param file_digest (str): 摘要
        :param size (int): 字节长度
        :param task_id (UUID): 原任务
        """
        launches.append(task_id)
        if len(launches) == 2:
            second_started.set()
        original_wrapper(files, user_id, file_digest, size, task_id)

    monkeypatch.setattr(PrivateObjectFiles, "delete_committed", pause)
    monkeypatch.setattr(object_module, "delete_with_receipt", start)
    first_store, second_store = (
        PrivateObjectStore(tmp_path / "objects"),
        PrivateObjectStore(tmp_path / "objects"),
    )
    first = asyncio.create_task(
        first_store.delete_committed(owner, digest, len(b"retained content"), identity)
    )
    second: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        second = asyncio.create_task(
            second_store.delete_committed(owner, digest, len(b"retained content"), identity)
        )
        assert await asyncio.to_thread(second_started.wait, 3)
        await asyncio.sleep(0.02)
        assert not second.done() and len(deletes) == 1
    finally:
        release.set()
        await asyncio.wait_for(first, 5)
        if second is not None:
            await asyncio.wait_for(second, 5)
    assert deletes == [digest]
    assert await SkillContentDeletionWorker(database, first_store).process(identity) == "complete"
