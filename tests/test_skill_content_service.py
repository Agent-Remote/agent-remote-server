"""
验证真实数据库事务下的上传隔离、原子引用、租约与配额竞争。
"""

import asyncio
import io
import os
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry
from test_skill_storage import file_entry

from agent_remote_server.db import Base
from agent_remote_server.models import User
from agent_remote_server.models.skill_storage import (
    SkillContentObject,
    SkillContentUpload,
    SkillStorageUsage,
    SkillStoredTree,
    SkillTreeObjectReference,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@pytest.fixture
async def database(
    tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """
    使用真实外键的文件数据库，可指向独立 PostgreSQL 验证生产事务语义。

    :param tmp_path (Path): 临时数据库目录
    :param request (pytest.FixtureRequest): 延迟获取空数据库模板
    :return AsyncIterator[async_sessionmaker[AsyncSession]]: 独立事务工厂
    """
    external_url = os.environ.get("SKILL_TEST_DATABASE_URL")
    if external_url is None:
        shutil.copyfile(request.getfixturevalue("sqlite_schema"), tmp_path / "database.db")
    url = external_url or f"sqlite+aiosqlite:///{tmp_path}/database.db"
    engine = create_async_engine(url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def enforce_foreign_keys(connection: DBAPIConnection, record: ConnectionPoolEntry) -> None:
            """
            在每条连接启用 SQLite 外键，禁止只靠 ORM 模拟完整性。

            :param connection (DBAPIConnection): 驱动连接
            :param record (ConnectionPoolEntry): 池中连接记录
            """
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    if external_url is not None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def user(database: async_sessionmaker[AsyncSession]) -> UUID:
    """
    为测试创建与其他用例隔离的真实用户。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :return UUID: 用户标识
    """
    identity = uuid4()
    async with database.begin() as session:
        session.add(
            User(
                id=identity,
                username=str(identity),
                display_name="测试",
                role="user",
                status="active",
                password_hash="test-only",
            )
        )
    return identity


def service(
    session: AsyncSession, tmp_path: Path, policy: SkillStoragePolicy | None = None
) -> SkillContentService:
    """
    用相同持久化卷重建无内存权威状态的服务实例。

    :param session (AsyncSession): 本次事务
    :param tmp_path (Path): 持久化根目录
    :param policy (SkillStoragePolicy | None): 可选的小配额测试策略
    :return SkillContentService: 内容服务
    """
    return SkillContentService(
        session, PrivateObjectStore(tmp_path / "objects"), policy or SkillStoragePolicy()
    )


async def test_upload_requires_full_tree_and_hides_uncommitted_bytes(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    部分上传不可下载或发布，完成后对象、树、用量和保活引用一次提交。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    a, b = file_entry(b"first", path="a"), file_entry(b"second", path="b")
    manifest = SkillTreeManifest(entries=(a, b))
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(owner, "add", manifest, "package")
        upload_id = upload.id
    async with database.begin() as session:
        await service(session, tmp_path).put_file(owner, upload_id, a.sha256, io.BytesIO(b"first"))
    async with database.begin() as session:
        with pytest.raises(SkillContentError, match="content not found"):
            await service(session, tmp_path).read_file(
                owner, "package", manifest_digest(manifest), a.sha256, io.BytesIO()
            )
    with pytest.raises(FileNotFoundError):
        async with database.begin() as session:
            await service(session, tmp_path).complete(owner, upload_id)
    async with database.begin() as session:
        assert (
            await session.get(SkillStoredTree, (owner, "package", manifest_digest(manifest)))
            is None
        )
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None and usage.package_bytes == 0 and usage.package_reserved == 11
        await service(session, tmp_path).put_file(owner, upload_id, b.sha256, io.BytesIO(b"second"))
    async with database.begin() as session:
        tree = await service(session, tmp_path).complete(owner, upload_id)
        assert tree.digest == manifest_digest(manifest)
    async with database.begin() as session:
        tree = await service(session, tmp_path).complete(owner, upload_id)
        output = io.BytesIO()
        await service(session, tmp_path).read_file(owner, "package", tree.digest, b.sha256, output)
        assert output.getvalue() == b"second"
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None and usage.package_bytes == 11 and usage.package_reserved == 0
        refs = (
            await session.scalars(
                select(SkillTreeObjectReference).where(SkillTreeObjectReference.user_id == owner)
            )
        ).all()
        assert {ref.object_digest for ref in refs} == {a.sha256, b.sha256}


async def test_every_upload_and_download_operation_is_owner_scoped(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    其他用户不能借已知上传 ID、树或文件摘要取得内容或修改计划。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner, stranger = await user(database), await user(database)
    entry = file_entry(b"private")
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(owner, "add", manifest, "package")
        await svc.put_file(owner, upload.id, entry.sha256, io.BytesIO(b"private"))
        await svc.complete(owner, upload.id)
    async with database.begin() as session:
        svc = service(session, tmp_path)
        for operation in (
            svc.get(stranger, upload.id),
            svc.complete(stranger, upload.id),
            svc.put_file(stranger, upload.id, entry.sha256, io.BytesIO(b"private")),
        ):
            with pytest.raises(SkillContentError) as error:
                await operation
            assert error.value.code == "UPLOAD_NOT_FOUND"
        with pytest.raises(SkillContentError) as error:
            await svc.read_file(
                stranger, "package", manifest_digest(manifest), entry.sha256, io.BytesIO()
            )
        assert error.value.code == "CONTENT_NOT_FOUND"
        other = await svc.begin(stranger, "add", manifest, "package")
        assert other.id != upload.id and other.reserved_bytes == len(b"private")
        with pytest.raises(FileNotFoundError):
            await svc.complete(stranger, other.id)


async def test_idempotency_and_content_dedup_are_distinct(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    原请求重试复用 ID，另一清单共享内容只计费一次，改写幂等键被拒绝。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    entry = file_entry(b"shared")
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        first = await svc.begin(owner, "key", manifest, "package")
        again = await svc.begin(owner, "key", manifest, "package")
        assert first.id == again.id
        with pytest.raises(SkillContentError) as error:
            await svc.begin(owner, "key", manifest, "state")
        assert error.value.code == "IDEMPOTENCY_CONFLICT"
        await svc.put_file(owner, first.id, entry.sha256, io.BytesIO(b"shared"))
        await svc.complete(owner, first.id)
    renamed = SkillTreeManifest(entries=(entry.model_copy(update={"path": "renamed"}),))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        second = await svc.begin(owner, "second", renamed, "package")
        assert second.reserved_bytes == 0
        await svc.complete(owner, second.id)
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None and usage.package_bytes == 6


async def test_independent_reservations_and_runtime_single_file_limits(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    并发计划消耗预留，运行状态不能被原始包暂存限制误拦截。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    policy = SkillStoragePolicy(
        package_file_bytes=8, user_package_bytes=10, user_staging_bytes=8, user_state_bytes=30
    )
    entry = file_entry(b"package")
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        svc = service(session, tmp_path, policy)
        await svc.begin(owner, "first", manifest, "package")
        with pytest.raises(SkillContentError) as error:
            await svc.begin(owner, "second", manifest, "package")
        assert error.value.code == "QUOTA_EXCEEDED"
        large = file_entry(b"runtime-state-is-large")
        runtime = await svc.begin(owner, "runtime", SkillTreeManifest(entries=(large,)), "state")
        assert runtime.reserved_bytes == large.size
        await svc.put_file(owner, runtime.id, large.sha256, io.BytesIO(b"runtime-state-is-large"))
        await svc.complete(owner, runtime.id)
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None and usage.package_reserved == 7 and usage.state_bytes == large.size


async def test_rolled_back_completion_is_retryable_without_partial_references(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    文件落盘与数据库提交间失败不会暴露半个树，原计划可安全重试。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    entry = file_entry(b"rollback")
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(owner, "key", manifest, "package")
        await svc.put_file(owner, upload.id, entry.sha256, io.BytesIO(b"rollback"))
    with pytest.raises(RuntimeError, match="crash"):
        async with database.begin() as session:
            await service(session, tmp_path).complete(owner, upload.id)
            raise RuntimeError("crash before commit")
    async with database.begin() as session:
        assert await session.get(SkillContentObject, (owner, "package", entry.sha256)) is None
        assert (
            await session.get(SkillStoredTree, (owner, "package", manifest_digest(manifest)))
            is None
        )
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None and usage.package_reserved == 8 and usage.package_bytes == 0
        assert (await service(session, tmp_path).get(owner, upload.id)).status == "staged"
        await service(session, tmp_path).complete(owner, upload.id)


async def test_expired_lease_cannot_publish_and_reservation_is_released_once(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    过期上传不得发布，重新受理计划时回收其预留且旧幂等记录仍可查询。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    manifest = SkillTreeManifest(entries=(file_entry(b"data"),))
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(owner, "old", manifest, "package")
        upload.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    async with database.begin() as session:
        svc = service(session, tmp_path)
        with pytest.raises(SkillContentError) as error:
            await svc.complete(owner, upload.id)
        assert error.value.code == "UPLOAD_NOT_ACTIVE"
        new = await svc.begin(owner, "new", manifest, "package")
        old = await svc.begin(owner, "old", manifest, "package")
        assert old.status == "expired" and old.reserved_bytes == 0
        assert new.reserved_bytes == 4
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None and usage.package_reserved == 4


async def test_concurrent_transactions_cannot_overbook_quota(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    两条独立连接同时请求最后一份额度，只允许一条事务提交。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    manifest = SkillTreeManifest(entries=(file_entry(b"123456"),))
    policy = SkillStoragePolicy(user_package_bytes=10)

    async def reserve(key: str) -> str:
        """
        在独立事务中竞争配额。

        :param key (str): 本次幂等键
        :return str: 受理结果或业务错误码
        """
        try:
            async with database.begin() as session:
                await service(session, tmp_path, policy).begin(owner, key, manifest, "package")
            return "accepted"
        except SkillContentError as error:
            return error.code

    assert sorted(await asyncio.gather(reserve("a"), reserve("b"))) == [
        "QUOTA_EXCEEDED",
        "accepted",
    ]
    async with database() as session:
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None and usage.package_reserved == 6


async def test_tree_reference_foreign_keys_protect_owner_and_objects(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    数据库本身阻止跨用户引用及删除仍被完整树引用的内容。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner, stranger = await user(database), await user(database)
    entry = file_entry(b"protected")
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(owner, "key", manifest, "package")
        await svc.put_file(owner, upload.id, entry.sha256, io.BytesIO(b"protected"))
        tree = await svc.complete(owner, upload.id)
    with pytest.raises(IntegrityError):
        async with database.begin() as session:
            session.add(
                SkillTreeObjectReference(
                    user_id=stranger,
                    category="package",
                    tree_digest=tree.digest,
                    object_digest=entry.sha256,
                )
            )
    with pytest.raises(IntegrityError):
        async with database.begin() as session:
            obj = await session.get(SkillContentObject, (owner, "package", entry.sha256))
            assert obj is not None
            await session.delete(obj)


async def test_deleting_object_and_metadata_forgery_cannot_be_referenced(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    回收中的对象不能取得新引用，相同摘要的伪造大小不能规避计量。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    entry = file_entry(b"protected")
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(owner, "key", manifest, "package")
        await svc.put_file(owner, upload.id, entry.sha256, io.BytesIO(b"protected"))
        await svc.complete(owner, upload.id)
    async with database.begin() as session:
        svc = service(session, tmp_path)
        bad = SkillTreeManifest(entries=(entry.model_copy(update={"size": 1}),))
        with pytest.raises(SkillContentError) as error:
            await svc.begin(owner, "fake", bad, "package")
        assert error.value.code == "CONTENT_METADATA_CONFLICT"
        obj = await session.get(SkillContentObject, (owner, "package", entry.sha256))
        assert obj is not None
        obj.status = "deleting"
        await session.flush()
        with pytest.raises(SkillContentError) as error:
            await svc.begin(owner, "retiring", manifest, "package")
        assert error.value.code == "CONTENT_UNAVAILABLE"


async def test_expiry_deletes_uncommitted_bytes_before_releasing_quota(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    过期清理不能只释放计量而把无限暂存留在磁盘。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    entry = file_entry(b"temporary")
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(owner, "expired", manifest, "package")
        await svc.put_file(owner, upload.id, entry.sha256, io.BytesIO(b"temporary"))
        upload.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    path = tmp_path / "objects" / str(owner) / entry.sha256[:2] / entry.sha256
    assert path.is_file()
    async with database.begin() as session:
        expired = await service(session, tmp_path).get(owner, upload.id)
        assert expired.status == "expired" and expired.reserved_bytes == 0
        assert not path.exists()
    async with database.begin() as session:
        again = await service(session, tmp_path).get(owner, upload.id)
        assert again.status == "expired"
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None and usage.package_reserved == 0


async def test_expiry_preserves_active_upload_and_cross_category_references(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    相同字节被有效租约或另一额度分类引用时必须保留。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    """
    owner = await user(database)
    entry = file_entry(b"shared")
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        first = await svc.begin(owner, "first", manifest, "package")
        active = await svc.begin(owner, "active", manifest, "package")
        await svc.put_file(owner, first.id, entry.sha256, io.BytesIO(b"shared"))
        first.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    async with database.begin() as session:
        svc = service(session, tmp_path)
        assert (await svc.get(owner, first.id)).status == "expired"
        await svc.complete(owner, active.id)
        runtime = await svc.begin(owner, "runtime", manifest, "state")
        await svc.put_file(owner, runtime.id, entry.sha256, io.BytesIO(b"shared"))
        runtime.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    async with database.begin() as session:
        svc = service(session, tmp_path)
        assert (await svc.get(owner, runtime.id)).status == "expired"
        output = io.BytesIO()
        await svc.read_file(owner, "package", manifest_digest(manifest), entry.sha256, output)
        assert output.getvalue() == b"shared"
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None and usage.package_bytes == 6 and usage.state_reserved == 0


async def test_expiry_failure_preserves_reservation(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    文件删除失败时保留租约计量，不把磁盘数据错误宣称为已回收。

    :param database (async_sessionmaker[AsyncSession]): 事务工厂
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 失败注入器
    """
    owner = await user(database)
    entry = file_entry(b"pending")
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(owner, "key", manifest, "package")
        await svc.put_file(owner, upload.id, entry.sha256, io.BytesIO(b"pending"))
        upload.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    async def fail_cleanup(
        self: PrivateObjectStore,
        owner_id: UUID,
        protected: set[str],
        expired: set[str],
        cutoff: datetime,
    ) -> None:
        """
        模拟底层卷不能完成删除和同步。

        :param owner_id (UUID): 所属用户
        :param protected (set[str]): 保活摘要
        :param expired (set[str]): 过期摘要
        :param cutoff (datetime): 回收截止时间
        """
        raise OSError("simulated disk failure")

    monkeypatch.setattr(PrivateObjectStore, "collect_uncommitted", fail_cleanup)
    with pytest.raises(OSError):
        async with database.begin() as session:
            await service(session, tmp_path).get(owner, upload.id)
    async with database() as session:
        usage = await session.get(SkillStorageUsage, owner)
        original = await session.get(SkillContentUpload, upload.id)
        assert usage is not None and usage.package_reserved == len(b"pending")
        assert original is not None and original.status == "staged"
