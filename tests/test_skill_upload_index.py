"""
验证逐对象索引的原始输入隔离、兼容回填、事务回滚与终态清理。
"""

import asyncio
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database as database
from test_skill_content_service import service, user
from test_skill_storage import file_entry

from agent_remote_server.models.skill_storage import SkillContentUpload, SkillUploadObject
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.build import protection


async def test_index_survives_new_service_without_loading_manifest(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    重建服务后仅加载逐对象声明，完整树提交后移除投影并保留原始输入。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有对象卷
    """
    owner = await user(database)
    first = file_entry(b"same bytes", path="a")
    last = file_entry(b"same bytes", path="b")
    manifest = SkillTreeManifest(entries=(first, last))
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(owner, "indexed", manifest, "state")
        identity = upload.id
        assert upload.object_index_version == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillUploadObject)
                .where(SkillUploadObject.upload_id == identity)
            )
            == 1
        )
    async with database.begin() as session:
        content = service(session, tmp_path)
        assert await content.prepare_file(owner, identity, first.sha256) == last
        metadata = await content.inspect_upload(owner, identity)
        assert "manifest_json" in inspect(metadata).unloaded
        assert await content.put_file(owner, identity, first.sha256, io.BytesIO(b"same bytes"))
        assert "manifest_json" in inspect(metadata).unloaded
    async with database.begin() as session:
        tree = await service(session, tmp_path).complete(owner, identity)
        assert tree.manifest_json == manifest.model_dump(mode="json")
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillUploadObject)
                .where(SkillUploadObject.upload_id == identity)
            )
            == 0
        )
        saved = await session.get(SkillContentUpload, identity)
        assert saved is not None and saved.status == "committed"
        assert saved.manifest_json == manifest.model_dump(mode="json")
    async with database.begin() as session:
        with pytest.raises(SkillContentError):
            await service(session, tmp_path).prepare_file(owner, identity, first.sha256)


@pytest.mark.parametrize("corrupt", [False, True])
async def test_legacy_index_backfill_is_atomic_and_bound_to_original_digest(
    database: async_sessionmaker[AsyncSession], tmp_path: Path, corrupt: bool
) -> None:
    """
    旧租约只能从完整原始清单回填，事务回滚或清单损坏不会发布部分索引。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有对象卷
    :param corrupt (bool): 是否破坏旧清单与原始摘要的一致性
    """
    owner = await user(database)
    entry = file_entry(b"retained")
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(owner, "legacy", manifest, "state")
        identity = upload.id
        await session.execute(
            delete(SkillUploadObject).where(SkillUploadObject.upload_id == identity)
        )
        upload.object_index_version = 0
        upload.object_index_count = None
        if corrupt:
            upload.manifest_json = SkillTreeManifest(entries=()).model_dump(mode="json")
    async with database() as session:
        content = service(session, tmp_path)
        if corrupt:
            with pytest.raises(SkillContentError, match="original upload manifest"):
                await content.prepare_file(owner, identity, entry.sha256)
        else:
            assert await content.prepare_file(owner, identity, entry.sha256) == entry
        await session.rollback()
    async with database.begin() as session:
        saved = await session.get(SkillContentUpload, identity)
        assert saved is not None and saved.object_index_version == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillUploadObject)
                .where(SkillUploadObject.upload_id == identity)
            )
            == 0
        )
        if not corrupt:
            await service(session, tmp_path).prepare_file(owner, identity, entry.sha256)
    if not corrupt:
        async with database.begin() as session:
            saved = await session.get(SkillContentUpload, identity)
            assert saved is not None and saved.object_index_version == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(SkillUploadObject)
                    .where(SkillUploadObject.upload_id == identity)
                )
                == 1
            )


async def test_expired_index_cannot_write_and_collection_removes_only_projection(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    已过期声明不能续权，原状态查询负责释放配额和删除派生索引。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有对象卷
    """
    owner, stranger = await user(database), await user(database)
    entry = file_entry(b"retained")
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(
            owner, "expire", SkillTreeManifest(entries=(entry,)), "state"
        )
        identity = upload.id
    async with database.begin() as session:
        with pytest.raises(SkillContentError, match="upload not found"):
            await service(session, tmp_path).prepare_file(stranger, identity, entry.sha256)
    async with database.begin() as session:
        await session.execute(
            update(SkillContentUpload)
            .where(SkillContentUpload.id == identity)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    async with database.begin() as session:
        with pytest.raises(SkillContentError):
            await service(session, tmp_path).put_file(
                owner, identity, entry.sha256, io.BytesIO(b"retained")
            )
    async with database.begin() as session:
        upload = await service(session, tmp_path).get(owner, identity)
        assert upload.status == "expired" and upload.reserved_bytes == 0
        assert upload.manifest_json["entries"]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillUploadObject)
                .where(SkillUploadObject.upload_id == identity)
            )
            == 0
        )


@pytest.mark.parametrize("field", ["user_id", "upload_id", "tree_digest", "scope"])
async def test_index_foreign_key_binds_complete_original_input(
    database: async_sessionmaker[AsyncSession], tmp_path: Path, field: str
) -> None:
    """
    数据库直接拒绝跨所有者、上传、完整树或额度分类的声明替换。

    :param database (async_sessionmaker[AsyncSession]): 真实外键事务工厂
    :param tmp_path (Path): 私有对象卷
    :param field (str): 被替换的原始身份字段
    """
    owner = await user(database)
    entry = file_entry(b"retained")
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(
            owner, "binding", SkillTreeManifest(entries=(entry,)), "state"
        )
        identity = upload.id
    value = uuid4() if field.endswith("_id") else "a" * 64 if field == "tree_digest" else "package"
    with pytest.raises(IntegrityError):
        async with database.begin() as session:
            await session.execute(
                update(SkillUploadObject)
                .where(SkillUploadObject.upload_id == identity)
                .values({field: value})
            )


async def test_concurrent_legacy_backfill_uses_original_user_lock(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    并发旧输入请求只构建一份完整索引，不能重复插入或更换上传身份。

    :param database (async_sessionmaker[AsyncSession]): 独立并发事务工厂
    :param tmp_path (Path): 私有对象卷
    """
    owner = await user(database)
    entry = file_entry(b"concurrent")
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(
            owner, "legacy", SkillTreeManifest(entries=(entry,)), "state"
        )
        identity = upload.id
        await session.execute(
            delete(SkillUploadObject).where(SkillUploadObject.upload_id == identity)
        )
        upload.object_index_version = 0
        upload.object_index_count = None

    async def prepare() -> None:
        """
        使用独立事务取得同一声明。
        """
        async with database.begin() as session:
            assert (
                await service(session, tmp_path).prepare_file(owner, identity, entry.sha256)
                == entry
            )

    await asyncio.gather(prepare(), prepare())
    async with database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillUploadObject)
                .where(SkillUploadObject.upload_id == identity)
            )
            == 1
        )


@pytest.mark.parametrize("fault", ["digest", "path", "extra", "kind"])
async def test_corrupt_index_never_grants_a_file_write(
    database: async_sessionmaker[AsyncSession], tmp_path: Path, fault: str
) -> None:
    """
    选中声明仍须满足规范字段和请求摘要，损坏不会降级为重新猜测权限。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有对象卷
    :param fault (str): 损坏的单文件声明字段
    """
    owner = await user(database)
    entry = file_entry(b"retained")
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(
            owner, "corrupt", SkillTreeManifest(entries=(entry,)), "state"
        )
        identity = upload.id
        raw = entry.model_dump(mode="json")
        if fault == "digest":
            raw["sha256"] = "a" * 64
        elif fault == "path":
            raw["path"] = "../escape"
        elif fault == "extra":
            raw["arbitrary_path"] = "/private"
        else:
            raw = file_entry(b"retained").model_dump(mode="json") | {"kind": "directory"}
        await session.execute(
            update(SkillUploadObject)
            .where(SkillUploadObject.upload_id == identity)
            .values(entry_json=raw)
        )
    async with database.begin() as session:
        with pytest.raises(SkillContentError, match="declaration is invalid"):
            await service(session, tmp_path).put_file(
                owner, identity, entry.sha256, io.BytesIO(b"retained")
            )
    assert not (tmp_path / "objects" / str(owner) / entry.sha256[:2] / entry.sha256).exists()


async def test_complete_rechecks_canonical_identity_after_indexed_upload(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    索引和字节均存在也不能让已改变的完整清单发布为原始树。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有对象卷
    """
    owner = await user(database)
    entry = file_entry(b"retained")
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(
            owner, "original", SkillTreeManifest(entries=(entry,)), "state"
        )
        identity = upload.id
    async with database.begin() as session:
        await service(session, tmp_path).put_file(
            owner, identity, entry.sha256, io.BytesIO(b"retained")
        )
        await session.execute(
            update(SkillContentUpload)
            .where(SkillContentUpload.id == identity)
            .values(manifest_json=SkillTreeManifest(entries=()).model_dump(mode="json"))
        )
    async with database.begin() as session:
        with pytest.raises(SkillContentError, match="original upload manifest"):
            await service(session, tmp_path).complete(owner, identity)


async def test_retention_uses_complete_digest_index_and_refuses_missing_rows(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    保活从完整声明计数取得所有摘要，缺行整体失败而不是让未知字节变成可删除。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有对象卷
    """
    owner = await user(database)
    entry = file_entry(b"protected")
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(
            owner, "protected", SkillTreeManifest(entries=(entry,)), "state"
        )
        identity = upload.id
    async with database() as session:
        index = await SkillRetentionRepository(session, max_json_characters=1).load(owner)
        assert "manifest_json" in inspect(index.uploads[0]).unloaded
        assert protection(index, datetime.now(UTC)).reasons("blob", entry.sha256) == frozenset(
            {"upload_lease"}
        )
    async with database.begin() as session:
        await session.execute(
            delete(SkillUploadObject).where(SkillUploadObject.upload_id == identity)
        )
    async with database() as session:
        with pytest.raises(ValueError, match="incomplete upload retention"):
            await SkillRetentionRepository(session).load(owner)


async def test_legacy_retention_still_checks_complete_manifest_without_backfill(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    旧版只读保活仍完整解析清单并保护字节，不创建新索引或绕过 JSON 预算。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有对象卷
    """
    owner = await user(database)
    entry = file_entry(b"legacy protected")
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(
            owner, "legacy", SkillTreeManifest(entries=(entry,)), "state"
        )
        identity = upload.id
        await session.execute(
            delete(SkillUploadObject).where(SkillUploadObject.upload_id == identity)
        )
        upload.object_index_version, upload.object_index_count = 0, None
    async with database() as session:
        index = await SkillRetentionRepository(session).load(owner)
        assert protection(index, datetime.now(UTC)).reasons("blob", entry.sha256) == frozenset(
            {"upload_lease"}
        )
        assert index.uploads[0].object_index_version == 0
        with pytest.raises(ValueError, match="metadata budget"):
            await SkillRetentionRepository(session, max_json_characters=1).load(owner)
