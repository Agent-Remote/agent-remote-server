"""
以真实 PostgreSQL、完整物理文件和节点 HTTP 路径验证默认运行字节额度。
"""

import asyncio
import os
import shutil
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_byte_capacity_support import GIB, begin_directory, directory_fixture, upload_directory
from skill_publication_support import new_session
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared
from test_skill_storage import file_entry
from test_skill_upload_capacity import capacity_client

from agent_remote_server.models.skill_storage import (
    SkillContentObject,
    SkillContentUpload,
    SkillStorageUsage,
    SkillUploadObject,
)
from agent_remote_server.schemas.skill_finalizations import SkillFinalizationRequest
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_REMOTE_RUN_SKILL_UPLOAD_BYTE_CAPACITY") != "1",
    reason="requires explicit disposable PostgreSQL and at least 44 GiB free disk space",
)


async def assert_usage(state: RuntimeHarness, stored: int, reserved: int) -> None:
    """
    独立事务核对已保存字节和预留，并用实际对象行合计交叉验证。

    :param state (RuntimeHarness): 当前用户与独立事务工厂
    :param stored (int): 预期已登记运行字节
    :param reserved (int): 预期仍未完成的预留字节
    """
    async with state.database() as session:
        usage = await session.get(SkillStorageUsage, state.owner)
        assert usage is not None
        assert (usage.state_bytes, usage.state_reserved) == (stored, reserved)
        total = await session.scalar(
            select(func.sum(SkillContentObject.size)).where(
                SkillContentObject.user_id == state.owner,
                SkillContentObject.category == "state",
            )
        )
        assert total == stored


async def assert_rejected(
    client: AsyncClient, state: RuntimeHarness, manifest: SkillTreeManifest
) -> None:
    """
    默认配额拒绝必须返回稳定错误，且不留下半受理上传。

    :param client (AsyncClient): 已认证的真实网络客户端
    :param state (RuntimeHarness): 尚无收尾输入的精确快照
    :param manifest (SkillTreeManifest): 只超出目标额度的清单
    """
    async with state.database() as session:
        before = await session.scalar(
            select(func.count())
            .select_from(SkillContentUpload)
            .where(SkillContentUpload.user_id == state.owner)
        )
    payload = SkillFinalizationRequest(
        session_id=state.session, idempotency_key=str(uuid4()), manifest=manifest, unclean=False
    )
    response = await client.post(
        f"/api/v1/node/skill-snapshots/{state.snapshot}/finalization",
        json=payload.model_dump(mode="json"),
    )
    assert response.status_code == 413, response.text
    assert not response.json()["committed"]
    assert response.json()["errors"][0]["code"] == "QUOTA_EXCEEDED"
    async with state.database() as session:
        after = await session.scalar(
            select(func.count())
            .select_from(SkillContentUpload)
            .where(SkillContentUpload.user_id == state.owner)
        )
        assert after == before


async def assert_stored_files(
    state: RuntimeHarness, root: Path, manifest: SkillTreeManifest, upload_id: UUID
) -> None:
    """
    完整回执必须对应普通独立磁盘对象和已退休上传索引。

    :param state (RuntimeHarness): 原始所有者
    :param root (Path): 本次内容卷根目录
    :param manifest (SkillTreeManifest): 已经完成的原始清单
    :param upload_id (UUID): 应已退休的上传尝试
    """
    for entry in manifest.entries:
        if entry.kind != "file":
            continue
        path = root / "objects" / str(state.owner) / entry.sha256[:2] / entry.sha256
        info = path.lstat()
        assert path.is_file() and not path.is_symlink()
        assert info.st_size == entry.size and info.st_nlink == 1
        assert info.st_blocks * 512 >= entry.size
    async with state.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillUploadObject)
                .where(
                    SkillUploadObject.user_id == state.owner,
                    SkillUploadObject.upload_id == upload_id,
                )
            )
            == 0
        )
        upload = await session.get(SkillContentUpload, upload_id)
        assert upload is not None and upload.status == "committed" and upload.reserved_bytes == 0


async def test_default_directory_and_user_bytes_over_http(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    两份独立完整目录经正式节点路由填满二十 GiB，验证预留、持久化与超额拒绝。

    :param stopped (RuntimeHarness): 真实预约且模拟终态的第一份快照
    :param tmp_path (Path): 本次验收独占目录
    """
    policy = SkillStoragePolicy()
    assert (policy.checkpoint_bytes, policy.directory_bytes, policy.user_state_bytes) == (
        GIB,
        10 * GIB,
        20 * GIB,
    )
    assert shutil.disk_usage(tmp_path).free >= 44 * GIB, "byte acceptance needs 44 GiB free"
    second = await new_session(stopped, tmp_path)
    rejected = await new_session(stopped, tmp_path)
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None and usage.state_reserved == 0
        baseline_bytes = usage.state_bytes
    assert 0 < baseline_bytes < GIB
    await assert_usage(stopped, baseline_bytes, 0)
    start = time.monotonic()
    first_root, second_root = tmp_path / "source-1", tmp_path / "source-2"
    first_manifest = await asyncio.to_thread(directory_fixture, first_root, 1, 10 * GIB)
    second_manifest = await asyncio.to_thread(
        directory_fixture, second_root, 2, 10 * GIB - baseline_bytes
    )
    first_digests = {entry.sha256 for entry in first_manifest.entries if entry.kind == "file"}
    second_digests = {entry.sha256 for entry in second_manifest.entries if entry.kind == "file"}
    assert first_digests.isdisjoint(second_digests)
    print(f"byte_capacity_fixture_seconds={time.monotonic() - start:.3f}", flush=True)

    async with capacity_client(stopped, tmp_path) as client, asyncio.timeout(60 * 60):
        await assert_rejected(
            client,
            stopped,
            SkillTreeManifest(
                entries=(*first_manifest.entries, file_entry(b"x", path="z-directory-excess"))
            ),
        )
        oversized = list(first_manifest.entries)
        oversized[2] = oversized[2].model_copy(update={"size": oversized[2].size + 1})
        oversized[-1] = oversized[-1].model_copy(update={"size": oversized[-1].size - 1})
        await assert_rejected(client, stopped, SkillTreeManifest(entries=tuple(oversized)))
        await assert_usage(stopped, baseline_bytes, 0)

        first_base, first_upload = await begin_directory(client, stopped, first_manifest)
        second_base, second_upload = await begin_directory(client, second, second_manifest)
        await assert_usage(stopped, baseline_bytes, 20 * GIB - baseline_bytes)
        excess = SkillTreeManifest(entries=(file_entry(b"x", path="excess"),))
        await assert_rejected(client, rejected, excess)
        await assert_usage(stopped, baseline_bytes, 20 * GIB - baseline_bytes)

        await upload_directory(client, first_base, first_upload, first_manifest, first_root)
        await assert_usage(stopped, baseline_bytes + 10 * GIB, 10 * GIB - baseline_bytes)
        await assert_stored_files(stopped, tmp_path, first_manifest, first_upload)
        print(f"byte_capacity_first_complete_seconds={time.monotonic() - start:.3f}", flush=True)
        await upload_directory(client, second_base, second_upload, second_manifest, second_root)
        await assert_usage(stopped, 20 * GIB, 0)
        await assert_stored_files(stopped, tmp_path, second_manifest, second_upload)
        await assert_rejected(client, rejected, excess)
        await assert_usage(stopped, 20 * GIB, 0)
        print(
            f"byte_capacity_stored={20 * GIB} elapsed_seconds={time.monotonic() - start:.3f}",
            flush=True,
        )
