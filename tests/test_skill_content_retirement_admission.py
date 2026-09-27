"""
验证任一配额分类的删除标记会阻断共享文件复用，同时保留原始元数据受理查询。
"""

import io
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database as database
from test_skill_content_service import service, user
from test_skill_library import LibraryHarness
from test_skill_storage import file_entry

from agent_remote_server.models.skill_storage import (
    SkillContentObject,
    SkillContentUpload,
    SkillStorageUsage,
)
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_library import SkillAddRequest, SkillRuleRequest, SkillScope
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.skill_manager.storage.policy import ContentScope


@pytest.mark.parametrize("scope", ["package", "state"])
@pytest.mark.parametrize(
    "action", ["begin", "prepare", "put", "complete", "replay", "tree", "file", "preview"]
)
async def test_shared_deleting_file_blocks_all_content_admission_paths(
    database: async_sessionmaker[AsyncSession], tmp_path: Path, scope: ContentScope, action: str
) -> None:
    """
    只检查当前分类会漏掉同用户共享文件，所有实际读写入口都必须在相同用户锁内拒绝。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有内容卷
    :param scope (ContentScope): 本次被保护的内容入口分类
    :param action (str): 被检验的实际准入入口
    """
    owner = await user(database)
    data = b"---\nname: learning\ndescription: test\n---\nshared\n"
    entry = file_entry(data)
    manifest = SkillTreeManifest(entries=(entry,))
    opposite: ContentScope = "state" if scope == "package" else "package"
    async with database.begin() as session:
        svc = service(session, tmp_path)
        original_key = str(uuid4())
        original = await svc.begin(owner, original_key, manifest, scope)
        await svc.put_file(owner, original.id, entry.sha256, io.BytesIO(data))
        tree = await svc.complete(owner, original.id)
        other = await svc.begin(owner, str(uuid4()), manifest, opposite)
        await svc.complete(owner, other.id)
        staged = await svc.begin(owner, str(uuid4()), manifest, scope)
        original_id, staged_id, digest = original.id, staged.id, tree.digest
        marker = await session.get(SkillContentObject, (owner, opposite, entry.sha256))
        assert marker is not None
        marker.status = "deleting"
    async with database() as session:
        index = await SkillRetentionRepository(session).load(owner)
        clocks = {(row.category, row.digest): row.retention_released_at for row in index.trees}
        upload_count = len(index.uploads)
        usage = await session.get(SkillStorageUsage, owner)
        assert usage is not None
        quota = (
            usage.package_bytes,
            usage.state_bytes,
            usage.package_reserved,
            usage.state_reserved,
        )
    target = io.BytesIO()
    async with database.begin() as session:
        svc = service(session, tmp_path)
        with pytest.raises(SkillContentError) as error:
            match action:
                case "begin":
                    await svc.begin(owner, str(uuid4()), manifest, scope)
                case "prepare":
                    await svc.prepare_file(owner, staged_id, entry.sha256)
                case "put":
                    await svc.put_file(owner, staged_id, entry.sha256, io.BytesIO(data))
                case "complete":
                    await svc.complete(owner, staged_id)
                case "replay":
                    await svc.complete(owner, original_id)
                case "tree":
                    await svc.read_tree(owner, scope, digest)
                case "file":
                    await svc.read_file(owner, scope, digest, entry.sha256, target)
                case "preview":
                    await svc.validate_state_admission(owner, manifest)
        assert error.value.code == "CONTENT_UNAVAILABLE"
    assert not target.getvalue()
    async with database.begin() as session:
        svc = service(session, tmp_path)
        assert (await svc.get(owner, original_id)).status == "committed"
        assert (await svc.begin(owner, original_key, manifest, scope)).id == original_id
        index = await SkillRetentionRepository(session).load(owner)
        assert len(index.uploads) == upload_count
        assert {
            (row.category, row.digest): row.retention_released_at for row in index.trees
        } == clocks
        usage = await session.get(SkillStorageUsage, owner)
        upload = await session.get(SkillContentUpload, staged_id)
        assert usage is not None and upload is not None and upload.status == "staged"
        assert quota == (
            usage.package_bytes,
            usage.state_bytes,
            usage.package_reserved,
            usage.state_reserved,
        )
    path = tmp_path / "objects" / str(owner) / entry.sha256[:2] / entry.sha256
    assert path.read_bytes() == data


@pytest.mark.parametrize("installed", [False, True])
async def test_shared_deletion_blocks_package_digest_install_and_pin_but_not_receipts(
    database: async_sessionmaker[AsyncSession], tmp_path: Path, installed: bool
) -> None:
    """
    已有包摘要的安装与 pin 也须检查另一分类；原安装回执仍可重放且不授予新文件读取。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有卷
    :param installed (bool): 是否已有安装以检验 pin 与原受理恢复
    """
    owner = await user(database)
    library = LibraryHarness(database, tmp_path, owner)
    candidate = await library.candidate()
    request = SkillAddRequest(
        items=(candidate,),
        expected_generation=await library.generation(),
        idempotency_key=str(uuid4()),
    )
    receipt = await library.execute(request) if installed else None
    async with database.begin() as session:
        svc = service(session, tmp_path)
        manifest = await svc.read_tree(owner, "package", candidate.tree_digest)
        state = await svc.begin(owner, str(uuid4()), manifest, "state")
        await svc.complete(owner, state.id)
        marker = await session.get(SkillContentObject, (owner, "state", manifest.entries[0].sha256))
        assert marker is not None
        marker.status = "deleting"
    generation = await library.generation()
    with pytest.raises(SkillContentError) as error:
        if installed:
            info = await library.info()
            await library.execute(
                SkillRuleRequest(
                    command="pin",
                    scope=SkillScope(tools=("claude",)),
                    skill="learning",
                    revision=str(info.default_revision_id),
                    expected_generation=generation,
                    idempotency_key=str(uuid4()),
                )
            )
        else:
            await library.execute(request)
    assert error.value.code == "CONTENT_UNAVAILABLE"
    assert await library.generation() == generation
    if receipt is not None:
        assert await library.execute(request) == receipt


async def test_deleting_marker_never_crosses_user_storage_namespace(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    相同摘要的另一用户拥有独立文件和配额，不受本用户删除标记影响。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有内容卷
    """
    first, second = await user(database), await user(database)
    data = b"shared digest, separate owners"
    entry = file_entry(data)
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(first, str(uuid4()), manifest, "state")
        await svc.put_file(first, upload.id, entry.sha256, io.BytesIO(data))
        await svc.complete(first, upload.id)
        marker = await session.get(SkillContentObject, (first, "state", entry.sha256))
        assert marker is not None
        marker.status = "deleting"
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(second, str(uuid4()), manifest, "package")
        await svc.put_file(second, upload.id, entry.sha256, io.BytesIO(data))
        tree = await svc.complete(second, upload.id)
        target = io.BytesIO()
        await svc.read_file(second, "package", tree.digest, entry.sha256, target)
        assert target.getvalue() == data
