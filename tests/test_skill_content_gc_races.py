"""
真实 PostgreSQL 连接验证删除与准入串行，并覆盖 SQL 锁丢失后的迟到磁盘调用。
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_gc import preview, upload
from test_skill_content_gc_worker import pending_deletion
from test_skill_content_service import database as database
from test_skill_content_service import service, user
from test_skill_storage import file_entry

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.gc import SkillContentReclamationService
from agent_remote_server.services.skills.gc.worker import SkillContentDeletionWorker
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def require_postgres(database: async_sessionmaker[AsyncSession]) -> None:
    """
    本组必须使用独立真实 PostgreSQL 连接。

    :param database (async_sessionmaker[AsyncSession]): 当前数据库工厂
    """
    async with database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL connections")


async def test_uncommitted_mark_cannot_authorize_worker(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    独立 worker 看不到尚未提交的标记；外层回滚不会留下已经删除的文件。

    :param database (async_sessionmaker[AsyncSession]): 独立连接
    :param tmp_path (Path): 内容卷
    """
    await require_postgres(database)
    owner = await user(database)
    _, tree, digest = await upload(database, tmp_path, owner)
    plan = await preview(database, owner, tree)
    async with database() as session:
        result = await SkillContentReclamationService(session, SkillStoragePolicy()).apply(
            owner, plan
        )
        worker = SkillContentDeletionWorker(database, PrivateObjectStore(tmp_path / "objects"))
        assert await worker.process(result.deletion_ids[0]) == "missing"
        await session.rollback()
    assert (tmp_path / "objects" / str(owner) / digest[:2] / digest).is_file()
    async with database() as session:
        assert await session.get(SkillContentDeletion, result.deletion_ids[0]) is None
        await service(session, tmp_path).read_tree(owner, "state", tree.identity)


async def test_new_upload_waits_for_worker_physical_completion_and_commit(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    marker 校验后的实际磁盘等待仍持有用户锁，新上传只有终态提交后才能重新建立内容。

    :param database (async_sessionmaker[AsyncSession]): 独立连接
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 磁盘阶段栅栏
    """
    await require_postgres(database)
    owner, identity, digest = await pending_deletion(database, tmp_path)
    store = PrivateObjectStore(tmp_path / "objects")
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.delete_committed

    async def paused(user_id: UUID, file_digest: str, size: int, task_id: UUID) -> None:
        """
        真实重验后暂停，释放后仍调用原始持久化字节操作。

        :param user_id (UUID): 用户
        :param file_digest (str): 摘要
        :param size (int): 字节数
        :param task_id (UUID): 原任务
        """
        entered.set()
        await release.wait()
        await original(user_id, file_digest, size, task_id)

    monkeypatch.setattr(store, "delete_committed", paused)
    deletion = asyncio.create_task(SkillContentDeletionWorker(database, store).process(identity))
    writer_pid: list[int] = []
    ready = asyncio.Event()

    async def new_upload() -> None:
        """
        第二连接实际完成同摘要的新分类上传。
        """
        async with database.begin() as session:
            writer_pid.append(int(await session.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            svc = service(session, tmp_path)
            manifest = SkillTreeManifest(entries=(file_entry(b"retained content"),))
            accepted = await svc.begin(owner, str(uuid4()), manifest, "package")
            import io

            await svc.put_file(owner, accepted.id, digest, io.BytesIO(b"retained content"))
            await svc.complete(owner, accepted.id)

    writer: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        writer = asyncio.create_task(new_upload())
        await asyncio.wait_for(ready.wait(), 3)
        async with database() as observer:
            blocked = False
            for _ in range(100):
                if await observer.scalar(select(func.pg_blocking_pids(writer_pid[0]))):
                    blocked = True
                    break
                await asyncio.sleep(0.01)
            assert blocked and not writer.done()
    finally:
        release.set()
        assert await asyncio.wait_for(deletion, 5) == "complete"
        if writer is not None:
            await asyncio.wait_for(writer, 5)
    assert (
        tmp_path / "objects" / str(owner) / digest[:2] / digest
    ).read_bytes() == b"retained content"


async def test_lost_database_lock_and_late_old_io_cannot_delete_new_incarnation(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    杀掉真实 worker 连接后另一 worker 完成并重新上传，迟到的旧磁盘调用仍被卷回执挡住。

    :param database (async_sessionmaker[AsyncSession]): 独立连接
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 旧 worker 阶段栅栏
    """
    await require_postgres(database)
    owner, identity, digest = await pending_deletion(database, tmp_path)
    store = PrivateObjectStore(tmp_path / "objects")
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.delete_committed

    async def delayed(user_id: UUID, file_digest: str, size: int, task_id: UUID) -> None:
        """
        在已通过数据库重验、尚未进入字节层时暂停旧调用。

        :param user_id (UUID): 用户
        :param file_digest (str): 摘要
        :param size (int): 字节数
        :param task_id (UUID): 原任务
        """
        entered.set()
        await release.wait()
        await original(user_id, file_digest, size, task_id)

    monkeypatch.setattr(store, "delete_committed", delayed)
    old = asyncio.create_task(SkillContentDeletionWorker(database, store).process(identity))
    blocked_pid: list[int] = []
    ready = asyncio.Event()

    async def waiter() -> None:
        """
        用相同用户锁查明实际 blocker 连接，不依赖进程名或 SQL 正文猜测。
        """
        async with database.begin() as session:
            blocked_pid.append(int(await session.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            await SkillStorageRepository(session).lock_existing_usage(owner)

    waiting: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        waiting = asyncio.create_task(waiter())
        await asyncio.wait_for(ready.wait(), 3)
        async with database() as observer:
            blockers: list[int] = []
            for _ in range(100):
                blockers = (
                    await observer.scalar(select(func.pg_blocking_pids(blocked_pid[0]))) or []
                )
                if blockers:
                    break
                await asyncio.sleep(0.01)
            assert len(blockers) == 1
            assert await observer.scalar(select(func.pg_terminate_backend(blockers[0])))
        await asyncio.wait_for(waiting, 3)
        worker = SkillContentDeletionWorker(database, PrivateObjectStore(tmp_path / "objects"))
        assert await worker.process(identity) == "complete"
        await upload(database, tmp_path, owner)
    finally:
        release.set()
        with pytest.raises(DBAPIError):
            await asyncio.wait_for(old, 5)
        if waiting is not None:
            await asyncio.wait_for(waiting, 3)
    path = tmp_path / "objects" / str(owner) / digest[:2] / digest
    assert path.read_bytes() == b"retained content"
